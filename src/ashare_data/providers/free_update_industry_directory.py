"""Complete visible industry directories, with hierarchy and native valuation fields.

The public page is a current snapshot. Its three static containers are reconciled
independently; no historical membership or official classification release date
is inferred from the reception date.
"""
import math
import re

from ashare_data.providers.free_update_coverage import require

LEVELS = {"sw_index_first_info": 1, "sw_index_second_info": 2, "sw_index_third_info": 3}
URL = "https://legulegu.com/stockdata/sw-industry-overview"
MEASURES = {"peQuantile": "静态市盈率", "peTtmQuantile": "TTM(滚动)市盈率",
            "pbQuantile": "市净率", "gxlQuantile": "静态股息率"}


def number(text):
    value = text.strip()
    if value in {"", "-", "--"}:
        return None
    parsed = float(value)
    require(math.isfinite(parsed), "industry valuation must be finite or an explicit source null")
    return parsed


def parse(body, name):
    from bs4 import BeautifulSoup
    require(name in LEVELS, "unreviewed industry directory")
    soup = BeautifulSoup(body, "lxml")
    levels = {}
    for level in (1, 2, 3):
        containers = soup.find_all(id=f"level{level}Items")
        require(len(containers) == 1, "industry level container missing or duplicated")
        section = containers[0]
        cards = section.find_all("div", class_="lg-industries-item")
        require(0 < len(cards) <= 2000 and len(section.find_all("li", class_=f"level{level}Item")) == len(cards),
                "industry source list/card count mismatch")
        output = []
        for card in cards:
            codes = card.select(".lg-industries-item-chinese-title")
            labels = card.select(".lg-industries-item-number")
            require(len(codes) == len(labels) == 1, "industry source identity/label absent or duplicated")
            code = codes[0].get_text(strip=True)
            label = labels[0]
            direct_label = ''.join(str(n) for n in label.find_all(string=True, recursive=False)).strip()
            matched = re.fullmatch(r"(.+?)\((\d+)\)", direct_label)
            require(re.fullmatch(r"\d{6}\.SI", code) and card.get("id") == code and matched,
                    "industry identity or constituent count label invalid")
            row = {"行业代码": code, "行业名称": matched[1], "成份个数": int(matched[2])}
            parents = label.select(".parent-industry-name")
            require(len(parents) == (0 if level == 1 else 1), "industry parent cardinality differs")
            if parents:
                parent = re.fullmatch(r"\[(.+)\]", parents[0].get_text(strip=True))
                require(parent, "industry parent label is invalid")
                row["上级行业"] = parent[1]
            groups = card.select(".lg-sw-industries-item-value .lg-sw-industries-pepb")
            require(len(groups) == len(MEASURES), "industry measure group count changed")
            for group in groups:
                matches = [key for key in MEASURES if group.select_one('.' + key) is not None]
                values = group.select("span.value")
                require(len(matches) == len(values) == 1 and MEASURES[matches[0]] not in row,
                        "industry measure label/value mismatch")
                row[MEASURES[matches[0]]] = number(values[0].get_text())
            output.append(row)
        require(len({r['行业代码'] for r in output}) == len(output), "duplicate industry code")
        if level > 1:
            parents = {r['行业名称'] for r in levels[level - 1]}
            require(all(r['上级行业'] in parents for r in output), "industry hierarchy contains an unknown parent")
        levels[level] = output
    selected = levels[LEVELS[name]]
    metadata = {"source": URL, "source_count": len(selected), "source_level": LEVELS[name],
        "level_counts": {str(k): len(v) for k, v in levels.items()}, "pages_received": 1,
        "complete_returned_window": True, "complete_history": False,
        "scope": "every card in the selected current static page container; not an official historical universe",
        "hierarchy_checked_against_same_response": True, "native_nulls_retained": True,
        "publication_date": None, "time_basis": "actual local receipt time; page gives no effective classification date"}
    return selected, metadata


def collect(capture, name, params):
    require(not params, "industry full-directory snapshot cannot be filtered")
    rows, capture.dataset_metadata = parse(capture.get(URL), name)
    return rows
