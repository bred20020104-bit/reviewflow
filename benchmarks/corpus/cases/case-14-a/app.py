def nonzero(items):
    for item in items:
        if item == 0:
            items.remove(item)
    return items
