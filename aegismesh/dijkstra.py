"""Dijkstra shortest paths, O((V+E) log V) with a binary heap, and FIB builder."""

import heapq


def shortest_paths(graph, source):
    """graph: {u: {v: cost}}. Returns (dist, prev)."""
    dist = {source: 0}
    prev = {}
    heap = [(0, source)]
    done = set()
    while heap:
        d, u = heapq.heappop(heap)
        if u in done:
            continue
        done.add(u)
        for v, w in graph.get(u, {}).items():
            nd = d + w
            if nd < dist.get(v, float("inf")):
                dist[v] = nd
                prev[v] = u
                heapq.heappush(heap, (nd, v))
    return dist, prev


def path_to(prev, source, dst):
    if dst != source and dst not in prev:
        return None
    path = [dst]
    while path[-1] != source:
        path.append(prev[path[-1]])
    return path[::-1]


def build_fib(graph, source):
    """Return {dst: (next_hop, total_cost)} for every reachable dst."""
    dist, prev = shortest_paths(graph, source)
    fib = {}
    for dst in dist:
        if dst == source:
            continue
        p = path_to(prev, source, dst)
        fib[dst] = (p[1], dist[dst])
    return fib
