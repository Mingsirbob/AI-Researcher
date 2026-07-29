import sqlite3

from app.research_store import ResearchStore


def _seed(store):
    store.upsert_security_name("000001.SZ", "平安银行", source="test")
    with store.connect() as conn:
        conn.execute("INSERT INTO announcement (announcement_id,security_code,source,source_sequence,title,report_date,published_at,source_url,metadata_hash,first_seen_at,last_seen_at,status,current_document_id) VALUES ('a1','000001.SZ','test','1','2025年年度报告','2026-03-01','2026-03-01','','hash','2026-03-01','2026-03-01','parsed','d1')")
        conn.execute("INSERT INTO announcement_document (document_id,announcement_id,version,sha256,file_path,source_url,content_type,byte_size,fetched_at,page_count,text_char_count,extraction_method,text_layer_status) VALUES ('d1','a1',1,'sha','documents/a.pdf','','application/pdf',1,'2026-03-01',1,20,'test','ok')")
        conn.execute("INSERT INTO document_chunk (chunk_id,document_id,chunk_index,page_start,page_end,text,text_sha256,char_count) VALUES ('c1','d1',0,1,1,'营业收入增长，经营现金流改善','text-sha',14)")
        try:
            conn.execute("INSERT INTO document_chunk_fts VALUES ('c1','000001.SZ','2025年年度报告','营业收入增长，经营现金流改善')")
        except sqlite3.OperationalError:
            store.fts_available = False


def test_security_point_in_time_and_document_search(tmp_path):
    store = ResearchStore(tmp_path / "state.db", tmp_path / "documents")
    _seed(store)
    with store.connect() as conn:
        conn.execute("INSERT INTO security_attribute_history (id,security_code,attribute_name,attribute_value,effective_from,effective_to,observed_at,source) VALUES ('h1','000001.SZ','industry_l1','银行','2020-01-01',NULL,'2026-01-01','test')")
    master = store.security_at("000001.SZ", "2026-07-01")
    result = store.search_document_chunks("000001.SZ", "营业收入", as_of="2026-07-01")
    assert master["industry_l1"] == "银行"
    assert result["items"][0]["chunk_id"] == "c1"
    assert result["retrieval_method"] in {"fts5_bm25", "like_fallback"}
