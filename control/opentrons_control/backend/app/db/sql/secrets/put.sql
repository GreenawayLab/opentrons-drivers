INSERT INTO secrets (name, ciphertext, kind, updated_at)
VALUES (:name, :ciphertext, :kind, now())
ON CONFLICT (name) DO UPDATE
SET ciphertext = EXCLUDED.ciphertext,
    kind       = EXCLUDED.kind,
    updated_at = now();