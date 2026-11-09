def minimum(items):
    result = 0
    for value in items:
        result = min(result, value)
    return result
