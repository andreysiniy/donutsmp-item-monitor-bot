MINECRAFT_NAMESPACE = "minecraft:"


def normalize_item_id(value: str) -> str:
    return value.strip().lower().removeprefix(MINECRAFT_NAMESPACE)
