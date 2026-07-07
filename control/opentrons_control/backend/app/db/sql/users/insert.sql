INSERT INTO users (name, role, password_hash)
VALUES (:name, :role, :password_hash)
RETURNING id;