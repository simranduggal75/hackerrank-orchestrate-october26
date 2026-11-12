"""Log parser tests: every syntax, trace correlation, integrity markers, accounting of every line."""

import json
import unittest

from evidence import log_parser
from loaders import DATASET_DIR, load_dataset
from tests.helpers import CAPTURED, NEEDS_DATASET, TmpCase, digest

# ---- one formatter per syntax, so the same event can be written seven ways -------------


def _kv(d):
    return " ".join(f'{k}="{v}"' if " " in str(v) or "@" in str(v) else f"{k}={v}" for k, v in d.items())


def fmt_kv_level(ts, svc, trace, event, **kw):
    return f"{ts} level=INFO service={svc} trace_id={trace} event={event} {_kv(kw)}".rstrip()


def fmt_kv_sev(ts, svc, trace, event, **kw):
    return f"{ts} INFO service={svc} event={event} trace_id={trace} {_kv(kw)}".rstrip()


def fmt_json(ts, svc, trace, event, **kw):
    return f"{ts} " + json.dumps({"severity": "INFO", "logger": svc, "service": svc, "trace_id": trace, "event": event, **kw})


def fmt_bracket(ts, svc, trace, event, **kw):
    return f"{ts} [INFO] [{svc}] [{trace}] {event}: " + " ".join(f'{k}="{v}"' for k, v in kw.items())


def fmt_pipe(ts, svc, trace, event, **kw):
    return f"{ts} | INFO | {svc} | {trace} | {event} | " + " ".join(f'{k}="{v}"' for k, v in kw.items())


def fmt_syslog(ts, svc, trace, event, **kw):
    return f'{ts} <14>1 node-1 {svc} {trace} - - level=INFO event="{event}" ' + " ".join(f'{k}="{v}"' for k, v in kw.items())


def fmt_cloud(ts, svc, trace, event, **kw):
    inner = f'event=\\"{event}\\" ' + " ".join(f'{k}=\\"{v}\\"' for k, v in kw.items())
    return f'{ts} requestId={trace} level=INFO function={svc} message="{inner}"'


FORMATS = {"kv-level": fmt_kv_level, "kv-sev": fmt_kv_sev, "json": fmt_json, "bracket": fmt_bracket,
           "pipe": fmt_pipe, "syslog": fmt_syslog, "cloud": fmt_cloud}

START_TS, DONE_TS = "2026-09-01T08:00:00Z", "2026-09-01T08:00:03Z"


def lookup_pair(fmt, key="r.pl27.jr@example.test", resource="account_7947", rows="0", http="404", trace="tr-1"):
    return [fmt(START_TS, "account_service", trace, "profile_lookup_started", lookup_key=key, attempt="1"),
            fmt(DONE_TS, "account_service", trace, "profile_lookup_completed", resource_id=resource, rows=rows,
                http_status=http, duration_ms="70")]


class LineSyntaxTests(unittest.TestCase):
    def test_every_syntax_normalises_to_the_same_event(self):
        for name, fmt in FORMATS.items():
            with self.subTest(syntax=name):
                d = log_parser.parse_line(fmt(DONE_TS, "account_service", "tr-9", "profile_lookup_completed",
                                              resource_id="account_7947", rows="0", http_status="404"))
                self.assertEqual((d["ts"], d["event"], d["trace_id"], d["service"]),
                                 (DONE_TS, "profile_lookup_completed", "tr-9", "account_service"))
                self.assertEqual((d["resource_id"], d["rows"], d["http_status"]), ("account_7947", "0", "404"))

    def test_cloud_wrapper_is_unescaped(self):
        d = log_parser.parse_line(fmt_cloud(DONE_TS, "profile_service", "tr-1", "cache_lookup", result="miss"))
        self.assertEqual((d["event"], d["result"]), ("cache_lookup", "miss"))

    def test_quoted_values_may_contain_spaces_and_equals(self):
        d = log_parser.parse_line(f'{DONE_TS} level=INFO service=s event=message_read message_text="a b=c d"')
        self.assertEqual(d["message_text"], "a b=c d")

    def test_garbage_is_not_understood(self):
        for line in ("hello world", "2026-09-01T08:00:00Z just text", "2026-09-01T08:00:00Z {\"no\":\"event\"}"):
            self.assertIsNone(log_parser.parse_line(line), line)

    def test_bad_json_line_raises_for_the_caller_to_report(self):
        with self.assertRaises(ValueError):
            log_parser.parse_line("2026-09-01T08:00:00Z {broken")


class FactBuildingTests(TmpCase):
    def parse(self, lines, captured=CAPTURED, name="l.log"):
        return log_parser.parse(self.write(name, "\n".join(lines) + "\n"), self.ev(name, captured))

    def test_trace_key_becomes_a_traced_identifier_in_every_syntax(self):
        for name, fmt in FORMATS.items():
            with self.subTest(syntax=name):
                result = self.parse(lookup_pair(fmt))
                self.assertEqual(self.codes(result), [])
                fact = result.facts[0]
                self.assertEqual((fact.system, fact.record_type, fact.state, fact.resource_id, fact.observed_at),
                                 ("account_service", "USER_PROFILE", "NOT_FOUND", "account_7947", DONE_TS))
                traced = [i for i in fact.identifiers if i.via == "trace"]
                self.assertEqual([(i.field, i.value, i.kind) for i in traced], [("lookup_key", "r.pl27.jr@example.test", "email")])
                self.assertEqual(fact.provenance.locator, "line 2 (trace tr-1, key line(s) 1)")

    def test_a_trace_reporting_on_several_resources_gets_no_borrowed_key(self):
        lines = [fmt_kv_level(START_TS, "search_service", "tr-7", "index_query_started", match_key="r.pl27@example.test", attempt="1"),
                 fmt_kv_level(DONE_TS, "search_service", "tr-7", "index_query_completed", resource_id="search_428", result="hit", index="i"),
                 fmt_kv_level(DONE_TS, "search_service", "tr-7", "index_query_completed", resource_id="search_1874", result="hit", index="i")]
        result = self.parse(lines)
        self.assertEqual([f.identifiers for f in result.facts], [(), ()])  # the key is not copied onto every hit
        self.assertEqual(self.codes(result), ["AMBIGUOUS_TRACE", "AMBIGUOUS_TRACE"])

    def test_a_trace_with_one_resource_still_borrows_its_key(self):
        lines = [fmt_kv_level(START_TS, "search_service", "tr-8", "index_query_started", match_key="a@x.com", attempt="1"),
                 fmt_kv_level(DONE_TS, "search_service", "tr-8", "index_query_completed", resource_id="search_1", result="hit", index="i"),
                 fmt_kv_level(DONE_TS, "search_service", "tr-8", "cache_lookup", result="miss", cache="c")]
        self.assertEqual([i.value for i in self.parse(lines).facts[0].identifiers], ["a@x.com"])

    def test_identifiers_do_not_leak_across_traces(self):
        lines = lookup_pair(fmt_kv_level, key="a@x.com", resource="account_1", trace="tr-1") + \
                lookup_pair(fmt_kv_level, key="b@x.com", resource="account_2", trace="tr-2")
        facts = {f.resource_id: [i.value for i in f.identifiers] for f in self.parse(lines).facts}
        self.assertEqual(facts, {"account_1": ["a@x.com"], "account_2": ["b@x.com"]})

    def test_found_profile_with_correlation_key_hash(self):
        digest = "ab" * 32
        lines = [fmt_kv_level(DONE_TS, "analytics_platform", "tr-3", "contact_lookup_completed",
                              resource_id="analytics_9914", rows="1", email_opt_in="true", correlation_key=digest)]
        fact = self.parse(lines).facts[0]
        # An event whose schema does not define the record is an UNMAPPED_LOOKUP with unknown state:
        # no record type is inferred, and a lookup event alone does not establish current state.
        self.assertEqual((fact.record_type, fact.state, fact.attributes["email_opt_in"]), ("UNMAPPED_LOOKUP", "UNKNOWN", "true"))
        self.assertEqual([(i.field, i.kind) for i in fact.identifiers], [("correlation_key", "email_hash")])

    def test_literal_null_values_are_not_identifiers(self):
        lines = [fmt_kv_level(DONE_TS, "support_platform", "tr-4", "support_ticket_read_completed", resource_id="support_7973",
                              customer_reference="null", requester_name="null", requester_email="rn.5927@example.test",
                              requester_phone="null")]
        fact = self.parse(lines).facts[0]
        self.assertEqual([i.value for i in fact.identifiers], ["rn.5927@example.test"])
        self.assertEqual(fact.state, "IDENTIFIABLE")

    def test_state_rules(self):
        cases = [
            ("analytics_identity_scan", "analytics_platform", dict(resource_id="analytics_1", matched_value="a@x.com", direct_identifiers="1"), "IDENTIFIABLE"),
            ("analytics_profile_read", "analytics_platform", dict(resource_id="analytics_2", direct_identifiers="0", email="null", phone="null"), "ANONYMIZED"),
            ("restore_authorization_completed", "backup_service", dict(resource_id="backup_1", decision="deny", policy="privacy_deletion"), "BLOCKED"),
            ("restore_authorization_completed", "backup_service", dict(resource_id="backup_2", decision="allow", policy="x"), "ACTIVE"),
            ("restore_candidate_evaluated", "backup_service", dict(resource_id="backup_3", eligible="true", deletion_block="false"), "ACTIVE"),
            ("restore_candidate_evaluated", "backup_service", dict(resource_id="backup_4", eligible="true", deletion_block="true"), "BLOCKED"),
            ("restore_policy_read_completed", "backup_service", dict(resource_id="backup_5", restore_allowed="true", archive_present="true"), "ACTIVE"),
            ("connector_heartbeat", "integration_service", dict(resource_id="data_flow_1", enabled="false", scheduler_registered="true"), "DISABLED"),
            ("connector_config_loaded", "integration_service", dict(resource_id="data_flow_2", enabled="true", scheduler_registered="true"), "ACTIVE"),
            ("message_read", "support_platform", dict(resource_id="support_1", subject_ref="user_461", message_text="[REDACTED]"), "REDACTED"),
            ("message_pii_scan", "support_platform", dict(resource_id="support_2", matched_value="a@x.com", detector="direct_identifier"), "IDENTIFIABLE"),
            ("index_query_completed", "search_service", dict(resource_id="search_1", index="i", result="hit"), "FOUND"),
            ("index_query_completed", "search_service", dict(resource_id="search_2", index="i", result="miss"), "NOT_FOUND"),
            ("profile_lookup_completed", "account_service", dict(resource_id="account_1", rows="1", http_status="200"), "FOUND"),
            ("contact_query_completed", "support_replica", dict(resource_id="support_5", rows="1", lookup_key="n@x.com"), "UNKNOWN"),
            ("deletion_marker_check", "support_replica", dict(resource_id="support_5", marker_found="false", last_write=START_TS), "UNKNOWN"),
            ("retained_order_loaded", "legal_service", dict(resource_id="legal_hold_1", reason="tax", access="allowed"), "FOUND"),
            ("audit_record_read_completed", "security_service", dict(resource_id="audit_record_1", integrity_check="passed", rows="1"), "FOUND"),
        ]
        for event, svc, kw, expected in cases:
            with self.subTest(event=event, resource=kw["resource_id"]):
                fact = self.parse([fmt_kv_level(DONE_TS, svc, "tr-x", event, **kw)]).facts[0]
                self.assertEqual(fact.state, expected)

    def test_explicit_record_lookup_line(self):
        line = fmt_kv_sev(DONE_TS, "downstream_delivery_service", "tr-1", "record_lookup").replace("trace_id=tr-1", "seq=1") + \
               ' system="account_service" resource_id="account_022" record_type="USER_PROFILE" result="NOT_FOUND" subject_reference="zofia@x.com"'
        fact = self.parse([line]).facts[0]
        self.assertEqual((fact.system, fact.resource_id, fact.state, fact.identifiers[0].value),
                         ("account_service", "account_022", "NOT_FOUND", "zofia@x.com"))

    def test_record_lookup_with_unknown_result_or_missing_fields(self):
        base = f'{DONE_TS} INFO service=s event=record_lookup'
        bad = self.parse([base + ' system="a" resource_id="r_1" record_type="USER_PROFILE" result="MAYBE" subject_reference="x"',
                          base + ' system="a" record_type="USER_PROFILE"'])
        self.assertEqual(sorted(self.codes(bad)), ["BAD_RECORD", "UNKNOWN_STATE"])
        self.assertEqual(bad.facts[0].state, "UNKNOWN")


class AccountingAndIntegrityTests(TmpCase):
    def parse(self, lines, captured=CAPTURED):
        return log_parser.parse(self.write("l.log", "\n".join(lines) + "\n"), self.ev("l.log", captured))

    def test_every_line_is_a_fact_noise_unmapped_or_unparsed(self):
        lines = lookup_pair(fmt_kv_level) + [
            fmt_kv_level(START_TS, "dns_cache", "-", "cache_refresh", entries="5"),
            fmt_kv_level(START_TS, "attachment_preview", "tr-5", "thumbnail_failed", resource_id="support_7731"),
            fmt_kv_level(START_TS, "mystery", "tr-6", "weird_event"),
            "totally not a log line", "2026-09-01T08:00:00Z {broken json", ""]
        result = self.parse(lines)
        s = result.stats
        self.assertEqual(s["lines"], 7)  # the blank line is not counted
        self.assertEqual((s["facts"], s["noise_lines"], s["lines_unmapped"], s["lines_unparsed"]), (1, 2, 2, 2))
        self.assertEqual(s["facts"] + s["noise_lines"] + s["lines_unmapped"] + s["lines_unparsed"], s["lines"])
        self.assertEqual(sorted(self.codes(result)), ["UNMAPPED_EVENT", "UNMAPPED_EVENT", "UNPARSEABLE_LINE", "UNPARSEABLE_LINE"])

    def test_mapped_event_without_resource_is_reported(self):
        result = self.parse([fmt_kv_level(DONE_TS, "account_service", "tr-1", "profile_lookup_completed", rows="1")])
        self.assertEqual((self.codes(result), result.facts), (["NO_RESOURCE_ID"], []))

    def test_started_without_completed_before_capture_is_truncation(self):
        lines = [fmt_kv_level("2026-09-01T07:00:00Z", "log_router", "c-1", "export_started", expected_source_events="10")] + \
                lookup_pair(fmt_kv_level)
        result = self.parse(lines, captured="2026-09-01T11:59:59Z")
        self.assertIn("LOG_MAY_BE_TRUNCATED", self.codes(result))

    def test_started_without_completed_but_reaching_capture_is_only_informational(self):
        lines = [fmt_kv_level("2026-09-01T07:00:00Z", "log_router", "c-1", "export_started", expected_source_events="10")] + \
                lookup_pair(fmt_kv_level)
        result = self.parse(lines, captured=DONE_TS)
        self.assertEqual([(i.level, i.code) for i in result.issues], [("INFO", "NO_COMPLETION_MARKER")])

    def test_expected_vs_reported_counts_and_dropped_events(self):
        lines = [fmt_kv_level("2026-09-01T07:00:00Z", "log_router", "c-1", "export_started", expected_source_events="10"),
                 fmt_kv_level("2026-09-01T09:00:00Z", "log_router", "c-1", "export_completed", source_events="9",
                              auxiliary_events="4", dropped_auxiliary="2")]
        self.assertEqual(sorted(self.codes(self.parse(lines))), ["COUNT_MISMATCH", "DROPPED_EVENTS"])

    def test_line_after_file_capture_is_flagged_and_excluded_for_earlier_audits(self):
        result = self.parse(lookup_pair(fmt_kv_level), captured="2026-09-01T08:00:01Z")
        self.assertIn("LINE_AFTER_CAPTURE", self.codes(result))
        fact = result.facts[0]
        self.assertEqual(fact.observed_at, DONE_TS)
        self.assertFalse(fact.available_by("2026-09-01T08:00:02Z"))

    def test_bad_timestamp_is_rejected(self):
        line = "2026-13-45T99:00:00Z level=INFO service=s trace_id=t event=profile_lookup_completed resource_id=account_1 rows=1"
        result = self.parse([line])
        self.assertEqual(self.codes(result), ["BAD_TIMESTAMP"])
        self.assertEqual(result.facts, [])

    def test_missing_file(self):
        result = log_parser.parse(self.dir / "absent.log", self.ev("absent.log"))
        self.assertEqual(self.codes(result), ["UNREADABLE_FILE"])


@NEEDS_DATASET
class RealLogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = load_dataset()
        cls.ev = {e.file_id: e for e in cls.ds.evidence_files}

    def parse(self, file_id):
        return log_parser.parse(DATASET_DIR / self.ev[file_id].file_path, self.ev[file_id])

    def test_every_log_line_is_accounted_for_and_none_are_unparsed(self):
        for file_id, e in self.ev.items():
            if e.file_path.endswith(".log"):
                s = self.parse(file_id).stats
                self.assertEqual(s.get("lines_unparsed", 0), 0, file_id)
                self.assertEqual(s.get("facts", 0) + s.get("noise_lines", 0) + s.get("lines_unmapped", 0), s["lines"], file_id)

    def test_decoy_lookalike_email_is_carried_on_the_traced_search_fact(self):
        facts = {f.resource_id: f for f in self.parse("evidence_01").facts}
        decoy = facts["search_1874"]
        self.assertEqual((decoy.record_type, decoy.state), ("SEARCH_INDEX_ENTRY", "FOUND"))
        self.assertIn("f244251fefdc64b69254a9a865e1517f5be0000743ea7d2fdc0f5b3adb0fda5d", [digest(i.value) for i in decoy.identifiers])
        self.assertNotIn("7afbbca4e0e4d5228a422ca39f5453505d365849ba5e20a288a203f0ba505c3d", [digest(i.value.lower()) for i in decoy.identifiers])


if __name__ == "__main__":
    unittest.main()
