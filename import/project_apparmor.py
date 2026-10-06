"""Generate the complement of the exact installer directories of reviewed project profiles."""

from project_profiles import PROJECTS

NAMES = tuple(project.domain for project in PROJECTS.values())
RECOVERY_NAMES = tuple(
    name
    for project in PROJECTS.values()
    for name in (
        project.domain + "-stage",
        project.domain + "-previous",
    )
)


def denied_names(names=NAMES):
    trie = {}
    for name in names:
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
    result = ""
    for root, allowed in (
        ("custom_components", NAMES),
        (".config-sync-integrations", RECOVERY_NAMES),
    ):
        names = "{" + ",".join(denied_names(allowed)) + "}"
        path = "/homeassistant/" + root + "/" + names + "{,/,/**}"
        result += "  /homeassistant/" + root + "/ rw,\n"
        result += (
            "\n".join("  deny " + path + " " + modes + "," for modes in ("rwmlkx", "a"))
            + "\n"
        )
    return result
