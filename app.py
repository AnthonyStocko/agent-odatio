"""
IHM de l'agent fonctionnel Odatio (Streamlit) : onglets Assistant et Configuration.

  venv\\Scripts\\python.exe -m streamlit run app.py      (ou double-clic sur lancer.bat)
"""
import streamlit as st

from odatio_agent import config, ihm_assistant, ihm_configuration

st.set_page_config(page_title="Agent Odatio", page_icon="🔎", layout="centered")


@st.cache_resource
def _initialiser() -> bool:
    config.charger_secrets()
    return True


_initialiser()

page_configuration = st.Page(ihm_configuration.page, title="Configuration", icon=":material/settings:",
                             url_path="configuration")
page_assistant = st.Page(lambda: ihm_assistant.page(page_configuration), title="Assistant",
                         icon=":material/forum:", url_path="assistant", default=True)
st.navigation([page_assistant, page_configuration], position="top").run()
