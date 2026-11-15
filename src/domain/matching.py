"""Party, PO and line resolution against trusted snapshot records (SPEC.md §3-5).

Resolution order: parties first, then the PO within those parties, then each
invoice line within that PO. Trusted identifiers and curated aliases/mappings
resolve; RapidFuzz similarity only ranks suggestions and never selects.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from rapidfuzz import fuzz, process

from .invoice import InvoiceLine, NormalizedInvoice, PrintedParty
from .money import ZERO, fmt_qty
from .normalize import norm_identifier, norm_text
from .snapshot import BillState, MappingKind, POHeader, POStatus, Party, Snapshot
from .results import Candidate, LineMatch, MatchResult, MatchStatus


# --------------------------------------------------------------------------- parties


def _fuzzy_party_candidates(name: str, parties: list[Party], snap: Snapshot, limit: int) -> list[Candidate]:
    """Rank plausible parties by name similarity. Suggestions only."""
    choices: dict[str, tuple[str, str]] = {}
    for p in parties:
        choices[f"{p.party_id}|legal"] = (p.party_id, p.legal_name)
        for a in snap.aliases_for(p.party_id):
            choices[f"{p.party_id}|{a.alias}"] = (p.party_id, a.alias)
    scored = process.extract(
        norm_text(name),
        {k: norm_text(v[1]) for k, v in choices.items()},
        scorer=fuzz.token_sort_ratio,
        limit=None,
    )
    seen: dict[str, Candidate] = {}
    for _match, score, key in scored:
        pid, text = choices[key]
        if pid in seen or score < 50:
            continue
        party = next(p for p in parties if p.party_id == pid)
        seen[pid] = Candidate(
            candidate_id=pid,
            label=party.legal_name,
            reasons=[f"Printed name resembles {text!r} (similarity {score:.0f}/100, suggestion only)"],
        )
        if len(seen) >= limit:
            break
    return list(seen.values())


def resolve_party(printed: PrintedParty, parties: list[Party], snap: Snapshot, *, limit: int = 3) -> MatchResult:
    """Resolve one party (buyer bill-to, or supplier) using IDs and approved aliases."""
    version = f"{snap.snapshot_id} v{snap.version}"
    name = printed.name.value
    reg = printed.registration_id.value

    name_hits: list[tuple[Party, str]] = []
    if name:
        key = norm_text(name)
        for p in parties:
            if norm_text(p.legal_name) == key:
                name_hits.append((p, "legal name"))
            for a in snap.aliases_for(p.party_id):
                if norm_text(a.alias) == key:
                    name_hits.append((p, f"approved alias {a.alias!r} ({a.source})"))

    if not name and not reg:
        return MatchResult(status=MatchStatus.NOT_FOUND, issue_code="PARTY_MISSING",
                           basis="No printed name or identifier", source_version=version)

    if reg:
        id_hits = [p for p in parties if p.registration_id and norm_identifier(p.registration_id) == norm_identifier(reg)]
        if len(id_hits) > 1:
            return MatchResult(
                status=MatchStatus.AMBIGUOUS, issue_code="PARTY_ID_NOT_UNIQUE", source_version=version,
                basis=f"Identifier {reg} matches {len(id_hits)} master records",
                candidates=[Candidate(candidate_id=p.party_id, label=p.legal_name, reasons=[f"Registration ID {reg}"]) for p in id_hits],
            )
        if not id_hits:
            if name_hits:
                p, how = name_hits[0]
                return MatchResult(
                    status=MatchStatus.CONFLICT, issue_code="PARTY_ID_CONFLICT", source_version=version,
                    basis=f"Name matches {p.legal_name} by {how}, but printed identifier {reg} is not on that record",
                    candidates=[Candidate(candidate_id=p.party_id, label=p.legal_name, reasons=[f"Name match by {how}", f"Master ID {p.registration_id}, printed {reg}"])],
                )
            return MatchResult(
                status=MatchStatus.NOT_FOUND, issue_code="PARTY_NOT_FOUND", source_version=version,
                basis=f"Identifier {reg} and name {name!r} not found in master",
                candidates=_fuzzy_party_candidates(name, parties, snap, limit) if name else [],
            )
        p = id_hits[0]
        others = [h for h in name_hits if h[0].party_id != p.party_id]
        if others:
            o, how = others[0]
            return MatchResult(
                status=MatchStatus.CONFLICT, issue_code="PARTY_ID_CONFLICT", source_version=version,
                basis=f"Identifier {reg} belongs to {p.legal_name}, but the printed name matches {o.legal_name} ({how})",
                candidates=[
                    Candidate(candidate_id=p.party_id, label=p.legal_name, reasons=[f"Registration ID {reg}"]),
                    Candidate(candidate_id=o.party_id, label=o.legal_name, reasons=[f"Name match by {how}"]),
                ],
            )
        own = [h for h in name_hits if h[0].party_id == p.party_id]
        if own:
            how = own[0][1]
            return MatchResult(
                status=MatchStatus.EXACT if how == "legal name" else MatchStatus.CURATED_ALIAS,
                selected_id=p.party_id, source_version=version,
                basis=f"Registration ID {reg} and {how}",
                evidence=[f"registration_id={reg}", f"name={name}"],
            )
        if not name:
            return MatchResult(status=MatchStatus.EXACT, selected_id=p.party_id, source_version=version,
                               basis=f"Registration ID {reg} (no printed name)", evidence=[f"registration_id={reg}"])
        return MatchResult(
            status=MatchStatus.AMBIGUOUS, issue_code="PARTY_NAME_UNVERIFIED", source_version=version,
            basis=f"Identifier {reg} matches {p.legal_name}, but printed name {name!r} is not an approved variation",
            candidates=[Candidate(candidate_id=p.party_id, label=p.legal_name, reasons=[f"Registration ID {reg}", "Name variation not on approved alias list"])],
        )

    distinct = {h[0].party_id: h for h in name_hits}
    if len(distinct) == 1:
        p, how = next(iter(distinct.values()))
        return MatchResult(
            status=MatchStatus.EXACT if how == "legal name" else MatchStatus.CURATED_ALIAS,
            selected_id=p.party_id, source_version=version, basis=f"No identifier printed; matched by {how}",
            evidence=[f"name={name}"],
        )
    if len(distinct) > 1:
        return MatchResult(
            status=MatchStatus.AMBIGUOUS, issue_code="PARTY_AMBIGUOUS", source_version=version,
            basis=f"Printed name {name!r} matches several master records",
            candidates=[Candidate(candidate_id=p.party_id, label=p.legal_name, reasons=[f"Matched by {how}"]) for p, how in distinct.values()],
        )
    cands = _fuzzy_party_candidates(name or "", parties, snap, limit)
    return MatchResult(
        status=MatchStatus.AMBIGUOUS if cands else MatchStatus.NOT_FOUND,
        issue_code="PARTY_UNCONFIRMED" if cands else "PARTY_NOT_FOUND",
        source_version=version, candidates=cands,
        basis=f"Printed name {name!r} has no identifier and is not an approved alias",
    )


# --------------------------------------------------------------------------- items


@dataclass(frozen=True)
class ItemIdentity:
    sku: str | None
    method: str
    status: MatchStatus
    conflict: str | None = None


def identify_item(line: InvoiceLine, supplier_id: str, snap: Snapshot) -> ItemIdentity:
    """Trusted item identity from internal SKU, supplier code mapping or curated description alias."""
    strong: list[tuple[str, str, MatchStatus]] = []
    sku_text = line.internal_sku.value
    if sku_text:
        item = snap.item(norm_identifier(sku_text))
        if item:
            strong.append((item.sku, f"internal SKU {item.sku}", MatchStatus.EXACT))
    code = line.supplier_item_code.value
    if code:
        for m in snap.supplier_item_mappings:
            if m.supplier_id == supplier_id and m.kind == MappingKind.CODE and norm_identifier(m.value) == norm_identifier(code):
                strong.append((m.sku, f"supplier code {code} via mapping {m.mapping_id} v{m.version}", MatchStatus.CURATED_ALIAS))
    desc_hit = None
    desc = line.description.value
    if desc:
        for m in snap.supplier_item_mappings:
            if m.supplier_id == supplier_id and m.kind == MappingKind.DESCRIPTION and norm_text(m.value) == norm_text(desc):
                desc_hit = (m.sku, f"description alias via mapping {m.mapping_id} v{m.version}", MatchStatus.CURATED_ALIAS)

    found = strong + ([desc_hit] if desc_hit else [])
    skus = {s for s, _, _ in found}
    if len(skus) > 1:
        return ItemIdentity(None, "", MatchStatus.CONFLICT, conflict="Item evidence points to different SKUs: " +
                            "; ".join(f"{how} -> {s}" for s, how, _ in found))
    if found:
        sku, how, status = found[0]
        return ItemIdentity(sku, how, status)
    return ItemIdentity(None, "", MatchStatus.NOT_FOUND)


def _line_suggestions(line: InvoiceLine, po_number: str, snap: Snapshot, limit: int) -> list[Candidate]:
    desc = line.description.value
    if not desc:
        return []
    out = []
    for pl in snap.lines_of(po_number):
        item = snap.item(pl.sku)
        score = fuzz.token_set_ratio(norm_text(desc), norm_text(item.description if item else pl.sku))
        out.append((score, Candidate(
            candidate_id=f"{pl.po_number}/{pl.line_no}",
            label=f"Line {pl.line_no}: {pl.sku} ({item.description if item else ''})",
            reasons=[f"Description similarity {score:.0f}/100 (suggestion only; needs a verified mapping)"],
        )))
    out.sort(key=lambda t: -t[0])
    return [c for _, c in out[:limit]]


def _spec_conflicts(invoice_specs: dict[str, str], reference: dict[str, str]) -> list[str]:
    conflicts = []
    ref_by_key = {norm_text(k): v for k, v in reference.items()}
    for key, val in invoice_specs.items():
        ref = ref_by_key.get(norm_text(key))
        if ref is not None and norm_text(ref) != norm_text(val):
            conflicts.append(f"{key}: invoice {val!r} vs PO {ref!r}")
    return conflicts


def resolve_lines(invoice: NormalizedInvoice, po: POHeader, supplier_id: str, snap: Snapshot, *, limit: int = 3) -> list[LineMatch]:
    """Match each invoice line to a line of the selected PO (SPEC.md §5 hierarchy)."""
    po_lines = snap.lines_of(po.po_number)
    results: list[LineMatch] = []
    for line in invoice.lines:
        base = dict(source_line_id=line.source_line_id, po_number=po.po_number, invoice_unit=line.unit.value)
        ident = identify_item(line, supplier_id, snap)
        if ident.conflict:
            results.append(LineMatch(status=MatchStatus.CONFLICT, issue_code="ITEM_EVIDENCE_CONFLICT", detail=ident.conflict, **base))
            continue

        printed_no = line.printed_po_line.value
        selected = None
        method = ident.method
        if printed_no is not None:
            selected = next((pl for pl in po_lines if pl.line_no == printed_no), None)
            if selected is None:
                results.append(LineMatch(status=MatchStatus.NOT_FOUND, issue_code="PO_LINE_NOT_FOUND",
                                         detail=f"{po.po_number} has no line {printed_no}", **base))
                continue
            if ident.sku is None:
                results.append(LineMatch(
                    status=MatchStatus.AMBIGUOUS, issue_code="ITEM_UNVERIFIED",
                    detail=f"PO line {printed_no} is printed, but the item has no SKU, mapped code or approved description",
                    candidates=_line_suggestions(line, po.po_number, snap, limit), **base))
                continue
            if ident.sku != selected.sku:
                results.append(LineMatch(
                    status=MatchStatus.CONFLICT, issue_code="ITEM_LINE_CONFLICT",
                    detail=f"Printed PO line {printed_no} is {selected.sku}, but item evidence ({ident.method}) is {ident.sku}", **base))
                continue
            method = f"printed PO line {printed_no} + {ident.method}"
        else:
            if ident.sku is None:
                cands = _line_suggestions(line, po.po_number, snap, limit)
                results.append(LineMatch(
                    status=MatchStatus.AMBIGUOUS if cands else MatchStatus.NOT_FOUND, issue_code="ITEM_UNRESOLVED",
                    detail="No internal SKU, mapped supplier code or approved description; similarity suggestions need a human decision",
                    candidates=cands, **base))
                continue
            hits = [pl for pl in po_lines if pl.sku == ident.sku]
            if not hits:
                results.append(LineMatch(status=MatchStatus.NOT_FOUND, issue_code="ITEM_NOT_ON_PO", sku=ident.sku,
                                         detail=f"{ident.sku} is not ordered on {po.po_number}", **base))
                continue
            if len(hits) > 1:
                results.append(LineMatch(
                    status=MatchStatus.AMBIGUOUS, issue_code="PO_LINE_AMBIGUOUS", sku=ident.sku,
                    detail=f"{ident.sku} appears on {len(hits)} lines of {po.po_number}; the correct line or schedule is needed",
                    candidates=[Candidate(candidate_id=f"{h.po_number}/{h.line_no}", label=f"Line {h.line_no}",
                                          reasons=[f"Price {h.net_unit_price}, qty {fmt_qty(h.quantity)} {h.unit}"]) for h in hits], **base))
                continue
            selected = hits[0]

        item = snap.item(selected.sku)
        reference_specs = {**(item.specifications if item else {}), **selected.specifications}
        conflicts = _spec_conflicts(line.specifications, reference_specs)
        common = dict(po_line_no=selected.line_no, sku=selected.sku, method=method, base_unit=selected.unit, **base)
        if conflicts:
            results.append(LineMatch(status=MatchStatus.CONFLICT, issue_code="SPECIFICATION_CONFLICT",
                                     detail="; ".join(conflicts), **common))
            continue

        unit = line.unit.value
        if not unit:
            results.append(LineMatch(status=MatchStatus.AMBIGUOUS, issue_code="UNIT_MISSING", detail="Invoice unit is not stated", **common))
            continue
        factor, source = None, None
        if norm_identifier(unit) == norm_identifier(selected.unit):
            factor, source = Decimal(1), "same unit"
        else:
            conv = next((c for c in snap.unit_conversions if c.sku == selected.sku
                         and norm_identifier(c.from_unit) == norm_identifier(unit)
                         and norm_identifier(c.to_unit) == norm_identifier(selected.unit)), None)
            if conv:
                factor, source = conv.factor, conv.source
        if factor is None:
            results.append(LineMatch(status=MatchStatus.AMBIGUOUS, issue_code="UNIT_CONVERSION_REQUIRED",
                                     detail=f"No approved conversion from {unit} to {selected.unit} for {selected.sku}", **common))
            continue

        qty = line.quantity.value
        results.append(LineMatch(
            status=ident.status, conversion_factor=factor, conversion_source=source,
            base_quantity=(qty * factor) if qty is not None else None,
            detail=f"Matched to {selected.po_number} line {selected.line_no} ({selected.sku})", **common))
    return results


# --------------------------------------------------------------------------- purchase orders


def _po_line_balance(snap: Snapshot, po_number: str, line_no: int) -> tuple[Decimal, Decimal]:
    line = next(l for l in snap.lines_of(po_number) if l.line_no == line_no)
    billed = sum((b.quantity for b in snap.billed_lines_for(po_number, line_no, BillState.POSTED)), ZERO)
    received = sum((r.received_qty for r in snap.receipts_for(po_number, line_no)), ZERO)
    return line.quantity - billed, received - billed


def candidate_pos(invoice: NormalizedInvoice, buyer_id: str, supplier_id: str, snap: Snapshot,
                  *, currency: str, limit: int) -> tuple[list[Candidate], int, str]:
    """Scoped PO candidates when no PO reference is printed. Stable deterministic order."""
    inv_date: date | None = invoice.invoice_date.value
    note = "" if inv_date else "Invoice date unknown: order-date filter not applied"
    pool = [p for p in snap.po_headers
            if p.buyer_id == buyer_id and p.supplier_id == supplier_id and p.currency == currency
            and p.status == POStatus.OPEN and (inv_date is None or p.po_date <= inv_date)]
    ranked = []
    for po in pool:
        verified, unresolved, reasons = 0, 0, []
        for lm in resolve_lines(invoice, po, supplier_id, snap):
            if lm.resolved:
                verified += 1
                remaining, receipt_avail = _po_line_balance(snap, po.po_number, lm.po_line_no)
                reasons.append(f"Invoice {lm.source_line_id} -> line {lm.po_line_no} {lm.sku} by {lm.method}; "
                               f"{fmt_qty(remaining)} {lm.base_unit} ordered and unbilled, "
                               f"{fmt_qty(receipt_avail)} {lm.base_unit} received and unbilled")
            else:
                unresolved += 1
                reasons.append(f"Invoice {lm.source_line_id}: {lm.issue_code}")
        if verified == 0:
            continue
        reasons.append(f"Order date {po.po_date.isoformat()}, same buyer, supplier and currency")
        gap = (inv_date - po.po_date).days if inv_date else 0
        ranked.append(((-verified, unresolved, gap, po.po_number),
                       Candidate(candidate_id=po.po_number, label=f"{po.po_number} dated {po.po_date.isoformat()}", reasons=reasons)))
    ranked.sort(key=lambda t: t[0])
    cands = [c for _, c in ranked]
    return cands[:limit], max(0, len(cands) - limit), note


def resolve_po(invoice: NormalizedInvoice, buyer_id: str, supplier_id: str, snap: Snapshot,
               *, currency: str, limit: int) -> MatchResult:
    version = f"{snap.snapshot_id} v{snap.version}"
    ref = invoice.po_reference.value
    if ref:
        po = snap.po(ref)
        if po is None:
            return MatchResult(status=MatchStatus.NOT_FOUND, issue_code="PO_NOT_FOUND", source_version=version,
                               basis=f"Printed reference {ref!r} is not in snapshot {snap.snapshot_id}")
        if po.buyer_id != buyer_id or po.supplier_id != supplier_id:
            return MatchResult(
                status=MatchStatus.CONFLICT, issue_code="PO_PARTY_CONFLICT", source_version=version,
                basis=f"{po.po_number} belongs to buyer {po.buyer_id} / supplier {po.supplier_id}, "
                      f"not the confirmed {buyer_id} / {supplier_id}. No substitute PO was chosen.",
                candidates=[Candidate(candidate_id=po.po_number, label=po.po_number, reasons=["Printed reference"])])
        return MatchResult(status=MatchStatus.EXACT, selected_id=po.po_number, source_version=version,
                           basis=f"Printed reference {ref!r}; buyer and supplier agree",
                           evidence=[f"po_reference={ref}"])

    cands, more, note = candidate_pos(invoice, buyer_id, supplier_id, snap, currency=currency, limit=limit)
    if not cands:
        return MatchResult(status=MatchStatus.NOT_FOUND, issue_code="PO_NOT_FOUND", source_version=version,
                           basis="No PO reference printed and no open PO of this buyer and supplier has a verified item match. " + note)
    issue = "PO_CONFIRMATION_REQUIRED" if len(cands) == 1 and more == 0 else "PO_AMBIGUOUS"
    return MatchResult(status=MatchStatus.AMBIGUOUS, issue_code=issue, candidates=cands, more_candidates=more,
                       source_version=version,
                       basis=(f"No PO reference printed; {len(cands) + more} plausible open PO(s). "
                              "Confirmation with a delivery or procurement reference is required. " + note).strip())
