def parse_bool(text):
    if text not in ('true', 'false'):
        raise ValueError('invalid boolean')
    return text == 'true'
