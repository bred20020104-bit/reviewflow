def minimum(items):
    result = items[0]
    for value in items:
        result = min(result, value)
    return result
