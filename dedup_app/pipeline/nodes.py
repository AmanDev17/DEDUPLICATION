from collections import defaultdict
from typing import List, Dict, Set, Tuple

TOP_N_BITS_WD  = 8    # narrow buckets for aligned hashes
TOP_N_BITS_RC  = 6    # broader buckets for rotation-invariant hashes
MAX_PER_BUCKET = 500

def _top(h: int, n: int) -> int:
    return (h >> (64 - n)) & ((1 << n) - 1)

def build_candidate_pairs(pages: List[dict]) -> Set[Tuple[int, int]]:
    buckets: Dict[str, List[int]] = defaultdict(list)

    for idx, page in enumerate(pages):
        buckets[f"W{_top(page['wh'], TOP_N_BITS_WD):03x}"].append(idx)
        buckets[f"D{_top(page['dh'], TOP_N_BITS_WD):03x}"].append(idx)
        buckets[f"R{_top(page['rh'], TOP_N_BITS_RC):02x}"].append(idx)
        buckets[f"C{_top(page['ch'], TOP_N_BITS_RC):02x}"].append(idx)  # ← new

    final: Dict[str, List[int]] = {}
    for key, members in buckets.items():
        members = list(dict.fromkeys(members))
        if len(members) <= MAX_PER_BUCKET:
            final[key] = members
        else:
            hfield = {'W':'wh','D':'dh','R':'rh','C':'ch'}[key[0]]
            sub: Dict[str, List[int]] = defaultdict(list)
            for idx in members:
                sub_key = f"{key}_{(pages[idx][hfield] >> 48) & 0xFF:02x}"
                sub[sub_key].append(idx)
            for sk, sv in sub.items():
                final[sk] = sv[:MAX_PER_BUCKET]

    pairs: Set[Tuple[int, int]] = set()
    for members in final.values():
        for a in range(len(members)):
            for b in range(a + 1, len(members)):
                i, j = members[a], members[b]
                pairs.add((min(i, j), max(i, j)))
    return pairs
