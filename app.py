"""Invoice Review Workbench: Streamlit entry point.

Run: uv run streamlit run app.py
"""

import streamlit as st

st.set_page_config(page_title="Invoice Review Workbench", page_icon="🧾", layout="wide")

nav = st.navigation([
    st.Page("src/ui/process.py", title="Process", icon="📤", default=True),
    st.Page("src/ui/review.py", title="Review", icon="🔎"),
    st.Page("src/ui/history.py", title="History", icon="🗂️"),
])
nav.run()
