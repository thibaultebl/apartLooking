"""Floor extraction from free text (descriptions, titles, URL slugs).

Portals almost never publish the floor as a field — flatfox is the only source
with a real one. Everywhere else it is prose ("au 2ème étage", "rez inférieur")
or a URL slug, so it has to be read out of text.

Floors are integers: 0 is the rez-de-chaussée, negatives are below it, and TOP
stands in for "top floor, exact number unstated" (attique, dernier étage).
"""
from __future__ import annotations

import re
import unicodedata
from typing import Optional

# "Definitely upper, number unknown." Any value above the tallest plausible
# building works; the criterion only ever compares it against ground level.
TOP = 99

# German appears alongside French: comparis serves Swiss-German labels
# ("3. Etage", "EG", "Untergeschoss") even on French-language listings.
_UPPER = re.compile(r"\b(\d{1,2})\s*(?:er|eme|ere|e|te|ten)?\s*(?:etage|stock|og)\b")
_TOP = re.compile(r"\b(?:dernier etage|attique|combles|mansard\w*"
                  r"|dachgeschoss|dg|attika)\b")
# The leading \b on "rez" is load-bearing: without it every French future tense
# ("vous serez", "vous apprécierez") reads as a ground floor, which flagged 12
# of 25 sampled flats. Bare "rez" must stay an alternative of its own —
# immobilier.ch writes "au rez inférieur" — and "chauss\w*" absorbs the
# misspellings the portals actually ship ("rez-chausseee").
_GROUND = re.compile(
    r"\b(?:rez(?:\s*de\s*chauss\w*|\s*(?:inferieur|superieur|jardin))?"
    r"|rdc|plain\s*pied|sous\s*sol|entresol"
    r"|erdgeschoss|eg|untergeschoss|ug|souterrain)\b"
)


def _normalize(s: str) -> str:
    s = unicodedata.normalize("NFKD", (s or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", s)


def from_text(text: str) -> Optional[int]:
    """Floor named in the text, or None when it says nothing conclusive.

    An explicit étage wins over a ground-floor phrase, and that ordering is the
    whole point: "immeuble avec commerce au rez-de-chaussée" describes the
    building, not the flat, and must not delete a genuine third-floor listing.

    Where several floors are named we take the first. The only question asked of
    the result is "is this ground level?", so the imprecision is free.
    """
    t = _normalize(text)
    m = _UPPER.search(t)
    if m:
        return int(m.group(1))
    if _TOP.search(t):
        return TOP
    if _GROUND.search(t):
        return 0
    return None


def is_ground(floor: Optional[int]) -> bool:
    """Whether this is a ground-floor (or below) flat. Unknown is not ground."""
    return floor is not None and floor <= 0


def describe(floor: Optional[int]) -> str:
    """Human label for a Telegram message."""
    if floor is None:
        return "étage inconnu"
    if floor == TOP:
        return "dernier étage"
    if floor < 0:
        return "sous-sol"
    if floor == 0:
        return "rez-de-chaussée"
    return f"étage {floor}"


# Cases collected from live flatfox descriptions, immobilier.ch URL slugs and
# og:titles, and acheter-louer detail pages. Run `python -m watcher.floors`.
_CASES = [
    ("Appartement 4 pièces au 2ème étage - 1003 Lausanne", 2),
    ("Charmant 3 pièces au rez inférieur à Lausanne", 0),
    ("appartement-2-pieces-rez-chausseee", 0),
    ("grand-logement-2-pieces-11eme-etage-vue-lac", 11),
    ("appartement-3-pieces-2eme-etage-centre-lausanne", 2),
    ("avenue-leman-54-lausanne-5-pieces-1er-etage", 1),
    ("Dernier étage 2 pièces", TOP),
    ("charmant 2 pieces sous combles sous gare", TOP),
    ("logement-35-pieces-125-esprit-loft-plain-pied", 0),
    ("charmant-3-pieces-rez-inferieur-lausanne", 0),
    ("appartement-neuf-35-pieces-rez-chaussee", 0),
    ("Vous serez séduit par ce bien, vous apprécierez la vue", None),
    ("Immeuble avec commerce au rez-de-chaussée; appartement au 3ème étage", 3),
    ("Joli 3.5 pièces au rez-de-chaussée surélevé avec jardin", 0),
    ("Bel appartement neuf au cœur d'un quartier cherché", None),
    ("Appartement 3.5 pièces", None),
    # German labels, as comparis serves them.
    ("3. Etage", 3),
    ("EG", 0),
    ("Untergeschoss", 0),
    ("2.5 Zimmer, 75 m², 1. Etage", 1),
    ("Dachgeschoss", TOP),
    ("4. Stock", 4),
    # "eg" must not fire inside an ordinary word — the \b guards do that.
    ("Gelegenheit in ruhiger Lage", None),
]


if __name__ == "__main__":
    failures = 0
    for text, want in _CASES:
        got = from_text(text)
        if got != want:
            failures += 1
            print(f"FAIL want={want!r} got={got!r}  {text}")
    print(f"{len(_CASES) - failures}/{len(_CASES)} cases pass")
    raise SystemExit(1 if failures else 0)
