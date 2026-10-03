"""Current direct WordPress database and Yoast metadata behavior."""

from __future__ import annotations


def get_wp_prefix(conn) -> str:
    cur = conn.cursor()
    cur.execute("""
        SELECT table_name
        FROM   information_schema.tables
        WHERE  table_schema = DATABASE()
          AND  table_name LIKE '%options'
        LIMIT 1
    """)
    row = cur.fetchone()
    prefix = row[0].replace("options", "") if row else "wp_"
    cur.close()
    return prefix


def upsert_postmeta(conn, prefix: str, post_id: int, key: str, value: str) -> None:
    cur = conn.cursor()
    table = f"{prefix}postmeta"
    cur.execute(
        f"""INSERT INTO {table} (post_id, meta_key, meta_value)
            VALUES (%s,%s,%s)
            ON DUPLICATE KEY UPDATE meta_value = VALUES(meta_value)
        """,
        (post_id, key, value),
    )
    conn.commit()
    cur.close()


def write_yoast_metadata(
    conn,
    post_id: int,
    *,
    focus_keyword: str,
    description: str,
    title: str,
    canonical_url: str | None,
    get_prefix=None,
    upsert=None,
) -> None:
    resolve_prefix = get_wp_prefix if get_prefix is None else get_prefix
    write_postmeta = upsert_postmeta if upsert is None else upsert
    prefix = resolve_prefix(conn)
    write_postmeta(conn, prefix, post_id, "_yoast_wpseo_focuskw", focus_keyword)
    write_postmeta(conn, prefix, post_id, "_yoast_wpseo_metadesc", description[:160])
    write_postmeta(conn, prefix, post_id, "_yoast_wpseo_title", title[:58])
    if canonical_url:
        write_postmeta(conn, prefix, post_id, "_yoast_wpseo_canonical", canonical_url)
