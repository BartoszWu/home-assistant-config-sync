"""Generate the complement of the exact installer directories of reviewed project profiles."""

from project_profiles import PROJECTS

NAMES = tuple(
    name
    for project in PROJECTS.values()
    for name in (
        project.domain,
        "." + project.domain + "-stage",
        "." + project.domain + "-previous",
    )
)


def denied_names():
    trie = {}
    for name in NAMES:
        node = trie
        for char in name:
            node = node.setdefault(char, {})
        node[""] = {}
    result = []

    def visit(node, prefix):
        children = sorted(k for k in node if k)
        if prefix and "" not in node:
            result.append(prefix)
        if children:
            result.append(prefix + "[^" + "".join(children) + "]*")
            for char in children:
                visit(node[char], prefix + char)
        elif "" in node:
            result.append(prefix + "?*")

    visit(trie, "")
    return result


def rules():
    names = "{" + ",".join(denied_names()) + "}"
    path = "/homeassistant/custom_components/" + names + "{,/,/**}"
    return (
        "  /homeassistant/custom_components/ rw,\n"
        + "\n".join("  deny " + path + " " + modes + "," for modes in ("rwmlkx", "a"))
        + "\n"
    )
