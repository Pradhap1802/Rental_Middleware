import re
from typing import Dict, List, Optional

# Groups of names/symbols that denote the SAME real-world unit under different
# spellings or abbreviations. RentAsst and Tally were confirmed live to disagree on
# both axes independently — RentAsst unit "Meter" (symbol "m") vs an existing Tally
# unit named "MTR", and RentAsst unit "Piece" (symbol "pc") vs an existing Tally unit
# named "Pieces" — so matching must check name-vs-name, name-vs-symbol, and
# symbol-vs-symbol, not just an exact name match. This list only needs to cover
# common rental/inventory units; anything not listed here still gets an exact
# (normalized) match via _unit_tokens_match, and anything neither listed nor exact
# falls through to "no match" — resolve_existing_unit_name then leaves the RentAsst
# name/symbol untouched and callers create it fresh, same as before this fix existed.
_UNIT_SYNONYM_GROUPS = [
    {"piece", "pieces", "pc", "pcs", "nos", "no", "number", "numbers", "unit", "units"},
    {"meter", "meters", "metre", "metres", "mtr", "mtrs", "m"},
    {"kilometer", "kilometers", "kilometre", "kilometres", "km", "kms"},
    {"centimeter", "centimeters", "centimetre", "centimetres", "cm", "cms"},
    {"kilogram", "kilograms", "kg", "kgs"},
    {"gram", "grams", "gm", "gms", "g"},
    {"litre", "litres", "liter", "liters", "ltr", "ltrs", "l"},
    {"millilitre", "millilitres", "milliliter", "milliliters", "ml", "mls"},
    {"box", "boxes", "bx"},
    {"packet", "packets", "pkt", "pkts", "pack", "packs"},
    {"set", "sets"},
    {"pair", "pairs", "pr", "prs"},
    {"roll", "rolls"},
    {"dozen", "dozens", "dz"},
    {"bag", "bags", "bg"},
    {"bundle", "bundles", "bdl"},
    {"day", "days"},
    {"hour", "hours", "hr", "hrs"},
    {"month", "months", "mth", "mths"},
    {"square feet", "square foot", "sq feet", "sq foot", "sqft", "sft"},
    {"square meter", "square meters", "square metre", "square metres", "sqm", "sqmtr"},
]
_UNIT_SYNONYM_LOOKUP: Dict[str, int] = {
    alias: idx for idx, group in enumerate(_UNIT_SYNONYM_GROUPS) for alias in group
}


def _normalize_unit_token(raw: Optional[str]) -> str:
    """Lowercases and strips everything but letters/digits so 'Sq. Ft.', 'sqft', and
    'SQ FT' all collapse to the same lookup key."""
    return re.sub(r"[^a-z0-9]", "", (raw or "").strip().lower())


def _unit_tokens_match(a: Optional[str], b: Optional[str]) -> bool:
    na, nb = _normalize_unit_token(a), _normalize_unit_token(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    group_a = _UNIT_SYNONYM_LOOKUP.get(na)
    group_b = _UNIT_SYNONYM_LOOKUP.get(nb)
    return group_a is not None and group_a == group_b


def resolve_existing_unit_name(
    rentasst_name: str, rentasst_symbol: str, existing_tally_units: List[Dict[str, str]]
) -> Optional[str]:
    """
    Finds a Tally UNIT master that already represents the same real-world unit as a
    RentAsst unit even when spelled/abbreviated differently — e.g. RentAsst
    "Meter"/symbol "m" against an existing Tally unit named "MTR", or RentAsst
    "Piece"/symbol "pc" against an existing Tally unit named "Pieces". Checks the
    RentAsst name and symbol against BOTH the existing unit's name and its symbol, so
    a match on any of the four combinations counts.

    Returns the EXISTING Tally unit's own NAME so callers reuse exactly that master
    (for BASEUNITS on a STOCKITEM, or a Physical Stock voucher's ACTUALQTY/BILLEDQTY)
    instead of creating a second, differently-spelled UNIT master for a unit Tally
    already has — confirmed live as a source of duplicate/orphaned UNIT masters and of
    a stock item's BASEUNITS pointing at one spelling while its reconciliation voucher
    used another, both contributing to Tally import failures and to the repeated
    master-creation bursts that preceded a native Memory Access Violation crash.

    Returns None if nothing in existing_tally_units matches — callers then fall back
    to creating a new unit under the RentAsst name, exactly as before this function
    existed.
    """
    for existing in existing_tally_units:
        ex_name = existing.get("name") or ""
        ex_symbol = existing.get("symbol") or ""
        if not ex_name:
            continue
        if (
            _unit_tokens_match(rentasst_name, ex_name)
            or _unit_tokens_match(rentasst_name, ex_symbol)
            or (rentasst_symbol and _unit_tokens_match(rentasst_symbol, ex_name))
            or (rentasst_symbol and _unit_tokens_match(rentasst_symbol, ex_symbol))
        ):
            return ex_name
    return None
