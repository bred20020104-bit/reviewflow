def lookup(connection, name):
    return connection.execute('SELECT id FROM users WHERE name = ?', (name,)).fetchall()
