from collections import defaultdict
from typing import List, Tuple, Dict


MAX_EDGES_PER_NODE = 10


# ── Sparse graph ──────────────────────────────────────────────────────────────

class SparseGraph:
    def __init__(self, n_nodes: int):
        self.n = n_nodes
        # adj[node] = list of (neighbour, score) sorted by score desc
        self.adj: Dict[int, List[Tuple[int, float]]] = defaultdict(list)

    def add_edge(self, i: int, j: int, score: float):
        self.adj[i].append((j, score))
        self.adj[j].append((i, score))

    def prune(self):
        """Keep only the MAX_EDGES_PER_NODE strongest edges per node."""
        for node in self.adj:
            neighbours = sorted(self.adj[node], key=lambda x: x[1], reverse=True)
            self.adj[node] = neighbours[:MAX_EDGES_PER_NODE]

    def edges(self):
        """Yield unique (i, j, score) triples."""
        seen = set()
        for node, neighbours in self.adj.items():
            for nb, score in neighbours:
                key = (min(node, nb), max(node, nb))
                if key not in seen:
                    seen.add(key)
                    yield key[0], key[1], score


def build_graph(n_pages: int, confirmed_pairs: List[Tuple[int, int, float]]) -> SparseGraph:
    g = SparseGraph(n_pages)
    for i, j, score in confirmed_pairs:
        g.add_edge(i, j, score)
    g.prune()
    return g


# ── Union-Find (path-compressed DSU) ─────────────────────────────────────────

class UnionFind:
    def __init__(self, n: int):
        self._parent = list(range(n))
        self._rank   = [0] * n

    def find(self, x: int) -> int:
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]   # path compression
            x = self._parent[x]
        return x

    def union(self, x: int, y: int):
        rx, ry = self.find(x), self.find(y)
        if rx == ry:
            return
        if self._rank[rx] < self._rank[ry]:
            rx, ry = ry, rx
        self._parent[ry] = rx
        if self._rank[rx] == self._rank[ry]:
            self._rank[rx] += 1


def cluster(graph: SparseGraph, n_pages: int) -> Dict[int, List[int]]:
    """
    Run Union-Find over graph edges.
    Returns { root_id: [page_indices in cluster] }
    Only clusters with ≥ 2 members (actual duplicates) are returned.
    """
    uf = UnionFind(n_pages)
    for i, j, _ in graph.edges():
        uf.union(i, j)

    groups: Dict[int, List[int]] = defaultdict(list)
    for node in range(n_pages):
        root = uf.find(node)
        groups[root].append(node)

    # Return only multi-member clusters (real duplicates)
    return {root: members for root, members in groups.items() if len(members) >= 2}
