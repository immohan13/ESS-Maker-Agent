"""Read Dataverse XML (e.g. msdyn_HRWorkdayAbsenceEnterTimeOff_MultiDay.xml) and surface
attributes the suggester might want to tweak. Write-back kept minimal & explicit."""
from __future__ import annotations

from pathlib import Path

from lxml import etree


def parse(path: str | Path) -> etree._ElementTree:
    return etree.parse(str(path))


def get_descriptions(tree: etree._ElementTree) -> list[dict[str, str]]:
    """Extract <description>/<displayName> pairs from common Dataverse element shapes."""
    root = tree.getroot()
    out: list[dict[str, str]] = []
    for el in root.iter():
        tag = etree.QName(el).localname.lower()
        if tag in {"description", "displayname", "instruction", "topicdescription"}:
            out.append({"tag": tag, "text": (el.text or "").strip(), "path": tree.getpath(el)})
    return out


def set_text_at(tree: etree._ElementTree, xpath: str, new_text: str) -> bool:
    nodes = tree.xpath(xpath)
    if not nodes:
        return False
    nodes[0].text = new_text
    return True


def write(tree: etree._ElementTree, path: str | Path) -> Path:
    p = Path(path)
    tree.write(str(p), pretty_print=True, xml_declaration=True, encoding="utf-8")
    return p
