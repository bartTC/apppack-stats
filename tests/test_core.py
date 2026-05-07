from __future__ import annotations

from apppack_stats import Stats, _write_csv, normalize_path
from apppack_stats.extractors import extract_request


def test_normalize_path_rewrites_ids_hashes_and_uuids() -> None:
    path = (
        "/orders/12345/items/"
        "0123456789abcdef0123456789abcdef/"
        "550e8400-e29b-41d4-a716-446655440000"
    )
    assert normalize_path(path) == "/orders/<id>/items/<hash>/<uuid>"


def test_extract_request_supports_both_log_shapes() -> None:
    assert extract_request(
        {
            "method": "GET",
            "path": "/health",
            "status": 200,
            "response_time_us": 1234,
        }
    ) == ("GET", "/health", 1234, 200)
    assert extract_request(
        {
            "request_method": "POST",
            "request_path": "/jobs/42",
            "response_status": 503,
            "response_time": 0.25,
        }
    ) == ("POST", "/jobs/42", 250000, 503)


def test_stats_ingest_aggregates_and_normalizes() -> None:
    stats = Stats(normalize=True)

    stats.ingest(
        '{"method":"GET","path":"/orders/123","status":200,"response_time_us":1000}'
    )
    stats.ingest(
        '{"method":"GET","path":"/orders/456","status":404,"response_time_us":2000}'
    )
    stats.ingest("not json")

    bucket = stats.buckets[("GET", "/orders/<id>")]
    assert stats.total_lines == 3
    assert stats.parsed_lines == 2
    assert bucket.count == 2
    assert bucket.errors_4xx == 1
    assert bucket.errors_5xx == 0
    assert bucket.avg_ms == 1.5


def test_write_csv_sorts_and_emits_expected_columns(capsys) -> None:
    stats = Stats(normalize=False)
    stats.buckets[("GET", "/slow")].add(3000, 500)
    stats.buckets[("GET", "/fast")].add(1000, 200)

    _write_csv(stats, "-", sort_col="avg", reverse=True)

    out = capsys.readouterr().out.strip().splitlines()
    assert out[0] == "method,path,count,avg_ms,p95_ms,max_ms,errors_4xx,errors_5xx"
    assert out[1].startswith("GET,/slow,1,3.0,3.0,3.0,0,1")
    assert out[2].startswith("GET,/fast,1,1.0,1.0,1.0,0,0")
