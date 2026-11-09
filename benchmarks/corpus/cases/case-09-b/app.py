import html

def render(text):
    return '<div>' + html.escape(text) + '</div>'
