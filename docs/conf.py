"""Sphinx configuration for the mssql-ocp-migrate documentation."""
import os
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent / "src"))
sys.path.insert(0, str(HERE))

import mssql_ocp_migrate  # noqa: E402
import _generate  # noqa: E402

_generate.generate()

project = "mssql-ocp-migrate"
author = "Karthikeyan Balasundaram"
copyright = "2026, Karthikeyan Balasundaram"
release = mssql_ocp_migrate.__version__
version = ".".join(release.split(".")[:2])

extensions = [
    "myst_parser",
    "sphinx.ext.autodoc",
    "sphinx.ext.autosummary",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
    "sphinx_copybutton",
    "sphinx_design",
]
myst_enable_extensions = ["colon_fence", "deflist", "fieldlist", "attrs_inline", "substitution"]
myst_heading_anchors = 3
myst_substitutions = {"version": release}
source_suffix = {".md": "markdown", ".rst": "restructuredtext"}
exclude_patterns = ["_build", "reference/_generated/*"]
templates_path = ["_templates"]

autodoc_member_order = "bysource"
autodoc_typehints = "description"
autodoc_default_options = {"members": True, "undoc-members": False, "show-inheritance": False}
copybutton_prompt_text = r"\$ |>>> "
copybutton_prompt_is_regexp = True
suppress_warnings = ["myst.header"]
nitpicky = False

html_theme = "pydata_sphinx_theme"
html_title = f"mssql-ocp-migrate {release}"
html_static_path = ["_static"]
html_css_files = ["custom.css"]
html_show_sourcelink = False
html_copy_source = False
html_theme_options = {
    "logo": {"text": "mssql-ocp-migrate"},
    "header_links_before_dropdown": 6,
    "navbar_align": "left",
    "show_toc_level": 2,
    "navigation_with_keys": False,
    "secondary_sidebar_items": ["page-toc"],
    "footer_start": ["copyright"],
    "footer_end": ["theme-version"],
    "icon_links": [{"name": "PyPI", "url": "https://pypi.org/project/mssql-ocp-migrate/",
                    "icon": "fa-solid fa-box"}],
}
html_sidebars = {"index": [], "whatsnew/index": []}
html_context = {"default_mode": "light"}


def _version_everywhere(app, docname, source):
    # MyST substitutions skip code blocks; replace the token before parsing so commands show the version
    source[0] = source[0].replace("{{ version }}", release)


def setup(app):
    app.connect("source-read", _version_everywhere)

# ---------------------------------------------------------------------------------------------
# MOM_PREVIEW=1: a dependency-light build (Sphinx "basic" HTML + the project's own stylesheet,
# no JavaScript) used to publish a static preview of these docs.
if os.environ.get("MOM_PREVIEW") == "1":
    extensions.remove("sphinx_copybutton")
    html_theme = "momtheme"
    html_theme_path = [str(HERE / "_preview")]
    html_theme_options = {}
    html_css_files = []
    html_sidebars = {}
    html_permalinks = False
