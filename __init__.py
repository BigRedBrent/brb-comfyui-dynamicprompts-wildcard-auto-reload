import dataclasses
import os
import re

NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]

LOG_PREFIX = "[Dynamic Prompts Wildcard Auto Reload]"

# Two fixes for Random Prompts:
#
# 1. Random Prompts reads the wildcard files once and keeps them in memory until ComfyUI restarts.
#    This checks the wildcards folder each time a Random Prompts node runs, and if any file was added,
#    removed or changed since its last run, it makes that node read the files again.
#
# 2. Spaces between a choice's weight and "::", like "{99   ::|text}", make Random Prompts treat the
#    weight as plain text. This removes those spaces, both in the prompt box and in wildcard files, so
#    weights can be lined up with spaces after the number. The same goes for a weight at the start of a
#    wildcard list entry, like "3 ::text". Spaces before a weight are never touched.

_patched = False
_patched_manager_classes = set()

# A number, then spaces, then "::". Only the spaces between the number and "::" are removed.
# (For a decimal weight like "1.1  ::", the digits after the dot are the number matched here.)
WEIGHT_WITH_SPACES = re.compile(r"(\d+)\s+::")

# A weight at the very start of a wildcard list entry, like "3 ::text": a number, optional spaces,
# then "::". Everything after the "::" is the entry's text, kept exactly.
ENTRY_WEIGHT = re.compile(r"^(\d+(?:\.\d+)?)\s*::(.*)$", re.DOTALL)


def tidy_weights(text):
    if not isinstance(text, str) or "::" not in text:
        return text
    return WEIGHT_WITH_SPACES.sub(r"\1::", text)


def entry_weight(text):
    # Returns (weight, text) if a wildcard list entry starts with a weight, otherwise None.
    match = ENTRY_WEIGHT.match(text)
    if not match:
        return None
    return float(match.group(1)), match.group(2)


def folder_signature(folder):
    # The name, modification time and size of every file in the folder, to tell whether anything changed.
    entries = []
    try:
        for dirpath, dirnames, filenames in os.walk(str(folder), followlinks=True):
            dirnames[:] = [name for name in dirnames if not name.startswith(".")]
            for name in filenames:
                path = os.path.join(dirpath, name)
                try:
                    info = os.stat(path)
                except OSError:
                    continue
                entries.append((path, info.st_mtime_ns, info.st_size))
    except OSError:
        pass
    entries.sort()
    return tuple(entries)


def tidy_wildcard_values(values):
    # Tidy the weights inside each wildcard entry, keeping each entry's own weight from the file.
    # An entry that starts with a weight, with or without spaces around it, gets that weight.
    items = []
    changed = False
    for item in values:
        if isinstance(item, str):
            weighted = entry_weight(item)
            if weighted is not None:
                from dynamicprompts.wildcards.item import WildcardItem

                weight, content = weighted
                tidied = WildcardItem(content=tidy_weights(content), weight=weight)
            else:
                tidied = tidy_weights(item)
        elif dataclasses.is_dataclass(item) and isinstance(getattr(item, "content", None), str):
            tidied_content = tidy_weights(item.content)
            tidied = item if tidied_content == item.content else dataclasses.replace(item, content=tidied_content)
        else:
            tidied = item
        if tidied is not item:
            changed = True
        items.append(tidied)
    if not changed:
        return values
    return type(values).from_items(items)


def patch_wildcard_manager(manager):
    # Tidy weights in every wildcard entry as Random Prompts reads it.
    manager_class = type(manager)
    if manager_class in _patched_manager_classes:
        return
    original_get_values = manager_class.get_values

    def get_values(self, wildcard):
        values = original_get_values(self, wildcard)
        try:
            return tidy_wildcard_values(values)
        except Exception as error:
            print(f"{LOG_PREFIX} Couldn't tidy weights in a wildcard: {error}")
            return values

    manager_class.get_values = get_values
    _patched_manager_classes.add(manager_class)


def find_random_prompts_base():
    # The shared base class of the Random Prompts nodes, found among the loaded nodes.
    try:
        import nodes
    except Exception:
        return None
    for node_class in nodes.NODE_CLASS_MAPPINGS.values():
        for base in getattr(node_class, "__mro__", ()):
            if base.__name__ == "DPAbstractSamplerNode" and hasattr(base, "get_prompt"):
                return base
    return None


def patch_random_prompts():
    global _patched
    if _patched:
        return True
    base = find_random_prompts_base()
    if base is None:
        return False

    original_get_prompt = base.get_prompt

    def get_prompt(self, *args, **kwargs):
        try:
            manager = getattr(self, "_wildcard_manager", None)
            if manager is not None:
                patch_wildcard_manager(manager)
                signature = folder_signature(self._find_wildcards_folder())
                if getattr(self, "_wildcard_auto_reload_signature", None) != signature:
                    first_check = not hasattr(self, "_wildcard_auto_reload_signature")
                    self._wildcard_auto_reload_signature = signature
                    manager.clear_cache()
                    # Start the prompt over so the new wildcard contents are used.
                    self._current_prompt = None
                    if not first_check:
                        print(f"{LOG_PREFIX} Wildcard files changed: reloaded them")
        except Exception as error:
            print(f"{LOG_PREFIX} Couldn't check the wildcard files: {error}")

        # Tidy weights in the text typed into the node.
        try:
            if "text" in kwargs:
                kwargs["text"] = tidy_weights(kwargs["text"])
            elif args:
                args = (tidy_weights(args[0]),) + tuple(args[1:])
        except Exception as error:
            print(f"{LOG_PREFIX} Couldn't tidy weights in the prompt: {error}")

        return original_get_prompt(self, *args, **kwargs)

    base.get_prompt = get_prompt
    _patched = True
    print(f"{LOG_PREFIX} Active: edited wildcard files are used on the next run, and spaces after weights are allowed")
    return True


def on_prompt(json_data):
    # Random Prompts may load after this add-on, so try again when a job is queued.
    patch_random_prompts()
    return json_data


if not patch_random_prompts():
    try:
        from server import PromptServer

        PromptServer.instance.add_on_prompt_handler(on_prompt)
    except Exception as error:
        print(f"{LOG_PREFIX} Not active: {error}")
