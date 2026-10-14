"""Commute links to Google Maps (open-source spec §1.8, Phase C).

Starting points replace the old "Commute from" stations and the arrive-by and leave-at times; the job page lists
every office of a researched employer and builds one Google Maps link per starting point and way of travelling. The
app sends nothing anywhere for a commute: the Transport for NSW, TomTom and OSRM code, its keys, its cache and
/api/commute/status are gone.
"""
from __future__ import annotations

import json
import re
import shutil
import socket
import subprocess
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from tests import helpers  # noqa: E402  (sets the test environment first)

import httpx  # noqa: E402
from sqlalchemy import text  # noqa: E402

from backend import app_settings, commute, export  # noqa: E402
from backend.db import CompanyProfile, Job, SessionLocal, UserProfile, Workspace, engine  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"


def no_network():
    """Patches that make any outside request fail the test: sockets, httpx's real transports and urllib. (The test
    client talks to the app in-process, through none of them.)"""
    def refuse(*args, **kwargs):
        raise AssertionError("an outside request was made")
    return [mock.patch.object(socket.socket, "connect", refuse),
            mock.patch.object(socket, "create_connection", refuse),
            mock.patch.object(httpx.HTTPTransport, "handle_request", refuse),
            mock.patch.object(httpx.AsyncHTTPTransport, "handle_async_request", refuse),
            mock.patch("urllib.request.urlopen", refuse)]


class Workspaced(unittest.TestCase):
    """A workspace of the first account's own for each test, with a client that sends its header."""

    def setUp(self):
        helpers.started()
        self.ws = helpers.new_workspace(f"Commute {self.id().rsplit('.', 1)[-1]}")
        self.h = {"X-Workspace": str(self.ws)}

    def add_job(self, **fields) -> int:
        values = {"workspace_id": self.ws, "url": f"https://commute.example/{self.ws}/{len(fields)}-{id(fields)}",
                  "title": "Delivery lead", "company": "Acme", "location_text": "Sydney NSW", "work_mode": "hybrid",
                  "status": "to_review", "detail_status": "full", "lat": -33.87, "lng": 151.21, **fields}
        with helpers.as_admin():
            db = SessionLocal()
            try:
                job = Job(**values)
                db.add(job)
                db.commit()
                return job.id
            finally:
                db.close()

    def put_points(self, c, points, **extra):
        profile = c.get("/api/profile", headers=self.h).json()
        return c.put("/api/profile", headers=self.h, json={**profile, "starting_points": points, **extra})


class StartingPointsTest(Workspaced):
    def test_saved_with_the_search_setup_and_checked(self):
        with helpers.client() as c:
            pin = c.post("/api/pins", headers=self.h, json={"label": "Mum's", "kind": "hybrid", "lat": -33.7,
                                                            "lng": 151.1, "radius_km": 5}).json()["id"]
            self.assertEqual(c.get("/api/profile", headers=self.h).json()["starting_points"], [])
            points = [{"label": "Station", "place": "  Gosford   Station NSW ", "use": "transit"},
                      {"label": "", "pin_id": pin, "use": "driving"},
                      {"place": "Café & Co, 1 Rue de l'Église", "use": "both"}]
            r = self.put_points(c, points)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["starting_points"], [
                {"label": "Station", "pin_id": None, "place": "Gosford Station NSW", "use": "transit"},
                {"label": "", "pin_id": pin, "place": None, "use": "driving"},
                {"label": "", "pin_id": None, "place": "Café & Co, 1 Rue de l'Église", "use": "both"}])
            # Any number (no limit, spec §1.16).
            many = [{"place": f"Stop {i}", "use": "transit"} for i in range(60)]
            self.assertEqual(len(self.put_points(c, many).json()["starting_points"]), 60)
            # Left out of a PUT: kept as they are.
            profile = c.get("/api/profile", headers=self.h).json()
            del profile["starting_points"]
            self.assertEqual(len(c.put("/api/profile", headers=self.h, json=profile).json()["starting_points"]), 60)

            other_ws = helpers.new_workspace("Someone else's pins")
            other_pin = c.post("/api/pins", headers={"X-Workspace": str(other_ws)},
                               json={"label": "Other", "kind": "home", "lat": -33.0, "lng": 151.0}).json()["id"]
            for bad in ([{"pin_id": pin, "place": "Both", "use": "both"}],     # a pin and a place
                        [{"label": "Nothing", "use": "both"}],                 # neither
                        [{"place": "   ", "use": "both"}],                     # a blank place
                        [{"place": "Somewhere", "use": "walking"}],            # not a use
                        [{"place": "Somewhere", "use": "both", "url": "x"}],   # an unknown field
                        [{"place": "x" * 201, "use": "both"}], [{"label": "x" * 81, "place": "y", "use": "both"}],
                        [{"pin_id": 0, "use": "both"}], [{"pin_id": "1; DROP", "use": "both"}],
                        [{"place": f"Stop {i}", "use": "transit"} for i in range(501)]):  # past the technical bound
                self.assertEqual(self.put_points(c, bad).status_code, 422, bad)
            self.assertEqual(len(c.get("/api/profile", headers=self.h).json()["starting_points"]), 60)
            # A pin that isn't one of this search's (another workspace's, a made-up number) is dropped, not stored,
            # and the rest of the search setup is still saved.
            r = self.put_points(c, [{"pin_id": other_pin, "use": "both"}, {"pin_id": 987654321, "use": "both"},
                                    {"place": "Kept", "use": "both"}], titles=["Saved anyway"])
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["titles"], ["Saved anyway"])
            self.assertEqual(r.json()["starting_points"],
                             [{"label": "", "pin_id": None, "place": "Kept", "use": "both"}])
            self.assertEqual(len(self.put_points(c, [{"place": f"Stop {i}", "use": "transit"}
                                                     for i in range(500)]).json()["starting_points"]), 500)

    def test_deleting_a_pin_drops_its_starting_points_and_saving_still_works(self):
        with helpers.client() as c:
            keep = c.post("/api/pins", headers=self.h, json={"label": "Kept", "kind": "hybrid", "lat": -33.6,
                                                             "lng": 151.1}).json()["id"]
            gone = c.post("/api/pins", headers=self.h, json={"label": "Gone", "kind": "hybrid", "lat": -33.5,
                                                             "lng": 151.0}).json()["id"]
            points = [{"label": "", "pin_id": keep, "place": None, "use": "driving"},
                      {"label": "", "pin_id": gone, "place": None, "use": "both"},
                      {"label": "Station", "pin_id": None, "place": "Gosford Station NSW", "use": "transit"}]
            self.assertEqual(self.put_points(c, points).status_code, 200)
            stale = c.get("/api/profile", headers=self.h).json()  # what an open page holds
            self.assertEqual(c.delete(f"/api/pins/{gone}", headers=self.h).status_code, 204)
            self.assertEqual(c.get("/api/profile", headers=self.h).json()["starting_points"],
                             [points[0], points[2]])
            # The page still sends the deleted pin's starting point with the next change: the save goes through.
            r = c.put("/api/profile", headers=self.h, json={**stale, "titles": ["Analyst"]})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual((r.json()["titles"], r.json()["starting_points"]), (["Analyst"], [points[0], points[2]]))

    def test_a_saved_start_whose_pin_is_gone_is_kept_and_does_not_block_saving(self):
        """A starting point already saved with a pin that is no longer there (as stored before pins took their
        starting points with them) stays, so the page can show it as deleted, and saving still works."""
        with helpers.as_admin():
            db = SessionLocal()
            db.add(UserProfile(id=self.ws, titles=["Analyst"], starting_points=[
                {"label": "Old", "pin_id": 987654321, "place": None, "use": "both"}]))
            db.commit()
            db.close()
        with helpers.client() as c:
            profile = c.get("/api/profile", headers=self.h).json()
            r = c.put("/api/profile", headers=self.h, json={**profile, "titles": ["Designer"]})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["starting_points"], profile["starting_points"])
            job = self.add_job()
            out = c.get(f"/api/jobs/{job}/commute", headers=self.h).json()
            self.assertEqual(out["starts"], [])
            self.assertTrue(any("deleted" in n for n in out["notes"]))

    def test_a_copied_workspace_starts_from_the_copied_pins(self):
        with helpers.client() as c:
            pin = c.post("/api/pins", headers=self.h, json={"label": "Parents", "kind": "home", "lat": -33.4,
                                                            "lng": 151.3}).json()["id"]
            other = c.post("/api/pins", headers=self.h, json={"label": "Gym", "kind": "hybrid", "lat": -33.8,
                                                              "lng": 151.0}).json()["id"]
            self.put_points(c, [{"label": "Parents by car", "pin_id": pin, "use": "driving"},
                                {"label": "", "pin_id": other, "use": "transit"},
                                {"label": "", "pin_id": 987654321, "use": "both"},  # already gone: not copied
                                {"label": "Station", "place": "Gosford Station NSW", "use": "transit"}])
            r = c.post("/api/workspaces", json={"name": "Commute copy", "copy_from": self.ws})
            self.assertEqual(r.status_code, 201, r.text)
            h2 = {"X-Workspace": str(r.json()["id"])}
            pins = {p["label"]: p["id"] for p in c.get("/api/pins", headers=h2).json()}
            self.assertEqual(set(pins), {"Parents", "Gym"})
            self.assertFalse({pin, other} & set(pins.values()))
            profile = c.get("/api/profile", headers=h2).json()
            self.assertEqual([p["pin_id"] for p in profile["starting_points"]], [pins["Parents"], pins["Gym"], None])
            r = c.put("/api/profile", headers=h2, json={**profile, "titles": ["In the copy"]})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["starting_points"], profile["starting_points"])
            job = self.add_job(workspace_id=int(h2["X-Workspace"]))
            out = c.get(f"/api/jobs/{job}/commute", headers=h2).json()
            self.assertEqual(out["starts"], [
                {"label": "Parents by car", "origin": "-33.400000,151.300000", "modes": ["driving"], "kind": "pin"},
                {"label": "Gym", "origin": "-33.800000,151.000000", "modes": ["transit"], "kind": "pin"},
                {"label": "Station", "origin": "Gosford Station NSW", "modes": ["transit"], "kind": "place"}])
            self.assertFalse(any("deleted" in n for n in out["notes"]))
            # The source workspace keeps its own.
            self.assertEqual(c.get("/api/profile", headers=self.h).json()["starting_points"][0]["pin_id"], pin)

    def test_the_old_times_are_gone_from_the_search_setup(self):
        with helpers.client() as c:
            profile = c.get("/api/profile", headers=self.h).json()
            for gone in ("commute_from", "commute_arrive_by", "commute_leave_at"):
                self.assertNotIn(gone, profile)

    def test_sealed_in_the_database(self):
        with helpers.client() as c:
            self.put_points(c, [{"label": "QZXSTART label", "place": "QZXSTART place", "use": "both"}])
        with engine.connect() as conn:
            (blob,) = conn.execute(text("SELECT starting_points FROM user_profile WHERE id = :i"), {"i": self.ws}).one()
        self.assertNotIn(b"QZXSTART", bytes(blob))
        self.assertIsNone(UserProfile.__table__.c.starting_points.server_default)

    def test_a_workspace_with_old_stations_and_times_shows_them_as_starting_points(self):
        """The old search setup, written as the code before starting points stored it: the stations in
        commute_from, the times in commute_arrive_by and commute_leave_at (sealed), no starting points."""
        with helpers.as_admin():
            db = SessionLocal()
            db.add(UserProfile(id=self.ws, titles=["Analyst"], commute_from=["Central Station", "Gosford Station, NSW",
                                                                               "central station"],
                               commute_arrive_by="08:30", commute_leave_at="17:30"))
            db.commit()
            db.close()
        job = self.add_job()
        with helpers.client() as c:
            c.post("/api/pins", headers=self.h, json={"label": "Home", "kind": "home", "lat": -33.4, "lng": 151.3})
            points = c.get("/api/profile", headers=self.h).json()["starting_points"]
            self.assertEqual(points, [
                {"label": "Central Station", "pin_id": None, "place": "Central Station", "use": "transit"},
                {"label": "Gosford Station, NSW", "pin_id": None, "place": "Gosford Station, NSW", "use": "transit"}])
            starts = c.get(f"/api/jobs/{job}/commute", headers=self.h).json()["starts"]
            self.assertEqual([(s["label"], s["modes"]) for s in starts],
                             [("Home", ["driving", "transit"]), ("Central Station", ["transit"]),
                              ("Gosford Station, NSW", ["transit"])])
            # Saving the search setup keeps them as starting points and empties the old columns.
            points[0]["use"] = "both"
            self.assertEqual(self.put_points(c, points).status_code, 200)
            self.assertEqual(c.get("/api/profile", headers=self.h).json()["starting_points"], points)
        with helpers.as_admin():
            db = SessionLocal()
            row = db.get(UserProfile, self.ws)
            self.assertEqual((row.commute_from, row.commute_arrive_by, row.commute_leave_at), (None, "", ""))
            db.close()

    def test_signed_out_nobody_reads_or_writes_them(self):
        with helpers.client() as c:
            c.post("/api/auth/logout")
            c.headers.pop("X-CSRF-Token", None)
            self.assertEqual(c.get("/api/profile", headers=self.h).status_code, 401)
            self.assertEqual(c.put("/api/profile", headers=self.h, json={"starting_points": []}).status_code, 401)


class CommuteEndpointTest(Workspaced):
    def test_places_for_the_links(self):
        job = self.add_job(office_text="Level 5, 1 Denison St, North Sydney NSW 2060", office_source="user",
                           office_lat=-33.84, office_lng=151.21)
        with helpers.client() as c:
            home = c.post("/api/pins", headers=self.h, json={"label": "Home", "kind": "home", "lat": -33.425,
                                                             "lng": 151.3417}).json()["id"]
            flat = c.post("/api/pins", headers=self.h, json={"label": "Flat", "kind": "home", "lat": -33.9,
                                                             "lng": 151.2}).json()["id"]
            gone = c.post("/api/pins", headers=self.h, json={"label": "Gone", "kind": "hybrid", "lat": -33.5,
                                                             "lng": 151.0}).json()["id"]
            self.put_points(c, [{"label": "Station", "place": "Gosford Station NSW", "use": "transit"},
                                {"label": "Flat by car", "pin_id": flat, "use": "driving"},  # takes the home pin's place
                                {"label": "", "pin_id": gone, "use": "both"}])
            c.delete(f"/api/pins/{gone}", headers=self.h)  # takes its starting point with it
            r = c.get(f"/api/jobs/{job}/commute", headers=self.h)
            self.assertEqual(r.status_code, 200, r.text)
            out = r.json()
            self.assertEqual(out["destination"], "Level 5, 1 Denison St, North Sydney NSW 2060")
            self.assertTrue(out["to_office"])
            self.assertEqual(out["starts"], [
                {"label": "Home", "origin": "-33.425000,151.341700", "modes": ["driving", "transit"], "kind": "home"},
                {"label": "Station", "origin": "Gosford Station NSW", "modes": ["transit"], "kind": "place"},
                {"label": "Flat by car", "origin": "-33.900000,151.200000", "modes": ["driving"], "kind": "pin"}])
            self.assertFalse(any("deleted" in n for n in out["notes"]))
            self.assertFalse({"arrive_by", "leave_at", "transit", "car", "links", "day"} & set(out))
            self.assertIsNotNone(home)

    def test_a_city_only_ad_links_to_the_city_and_says_so(self):
        job = self.add_job(location_text="Sydney NSW; Melbourne VIC")
        with helpers.client() as c:
            out = c.get(f"/api/jobs/{job}/commute", headers=self.h).json()
        self.assertEqual((out["destination"], out["to_office"], out["starts"]), ("Sydney NSW", False, []))
        self.assertTrue(any("only a city" in n for n in out["notes"]))

    def test_a_job_with_no_place_is_refused_plainly(self):
        job = self.add_job(location_text="", lat=None, lng=None)
        with helpers.client() as c:
            r = c.get(f"/api/jobs/{job}/commute", headers=self.h)
        self.assertEqual(r.status_code, 409)
        self.assertIn("Set the office", r.json()["detail"])

    def test_a_merged_copy_answers_for_its_main_job(self):
        main = self.add_job(office_text="1 Main St, Sydney NSW", office_source="user")
        copy = self.add_job(url="https://commute.example/copy", duplicate_of=main)
        with helpers.client() as c:
            self.assertEqual(c.get(f"/api/jobs/{copy}/commute", headers=self.h).json()["destination"],
                             "1 Main St, Sydney NSW")

    def test_who_may_call_it(self):
        job = self.add_job()
        other = helpers.make_user()
        with helpers.client(other.email) as c:  # another account's job is not found
            self.assertEqual(c.get(f"/api/jobs/{job}/commute", headers=self.h).status_code, 404)
            self.assertEqual(c.get(f"/api/jobs/{job}/commute").status_code, 404)
        with helpers.client() as c:
            mine = helpers.new_workspace("Another of mine")
            self.assertEqual(c.get(f"/api/jobs/{job}/commute", headers={"X-Workspace": str(mine)}).status_code, 404)
            c.post("/api/auth/logout")
            self.assertEqual(c.get(f"/api/jobs/{job}/commute", headers=self.h).status_code, 401)

    def test_no_endpoint_here_makes_an_outside_request(self):
        job = self.add_job(office_text="1 Denison St, North Sydney NSW", office_source="user",
                           office_lat=-33.84, office_lng=151.21)
        with helpers.client() as c:
            patches = no_network()
            for p in patches:
                p.start()
            try:
                pin = c.post("/api/pins", headers=self.h, json={"label": "Home", "kind": "home", "lat": -33.4,
                                                                "lng": 151.3}).json()["id"]
                self.assertEqual(self.put_points(c, [{"place": "Central Station", "use": "transit"},
                                                     {"pin_id": pin, "use": "driving"}]).status_code, 200)
                self.assertEqual(c.get("/api/profile", headers=self.h).status_code, 200)
                self.assertEqual(c.get(f"/api/jobs/{job}/commute", headers=self.h).status_code, 200)
                self.assertEqual(c.get(f"/api/jobs/{job}", headers=self.h).status_code, 200)
            finally:
                for p in patches:
                    p.stop()

    def test_the_commute_code_holds_no_outside_service(self):
        src = (ROOT / "backend" / "commute.py").read_text(encoding="utf-8")
        for gone in ("import httpx", "requests", "transport.nsw", "tomtom.com", "project-osrm", "_cached",
                     "commute_cache", "TFNSW_API_KEY", "TOMTOM_API_KEY"):
            self.assertNotIn(gone, src)


class RemovedTest(unittest.TestCase):
    def test_the_commute_services_endpoints_are_gone(self):
        with helpers.client() as c:
            for method, path in (("GET", "/api/commute/status"), ("GET", "/api/settings/keys"),
                                 ("PUT", "/api/settings/keys"), ("POST", "/api/settings/keys/test")):
                self.assertIn(c.request(method, path, json={}).status_code, (404, 405), path)

    def test_the_keys_are_retired_never_set(self):
        for name in ("TFNSW_API_KEY", "TOMTOM_API_KEY"):
            self.assertIn(name, app_settings.RETIRED_ENV_NAMES)
            self.assertNotIn(name, app_settings.ENV_RULES)
            with self.assertRaises(ValueError):
                app_settings.update_env_file(Path(helpers.scratch_folder()) / ".env", {name: "abcdefgh1234"})
        folder = Path(helpers.scratch_folder())
        env = folder / ".env"
        env.write_text("LLM_MODEL=m\nTFNSW_API_KEY=eyJ.secret-tfnsw\nexport TOMTOM_API_KEY=tomtom-secret\nA=1\n")
        self.assertEqual(app_settings.remove_retired_env(env), ["TFNSW_API_KEY", "TOMTOM_API_KEY"])
        self.assertEqual(env.read_text(), "LLM_MODEL=m\nA=1\n")

    def test_setup_and_the_example_settings_no_longer_ask_for_them(self):
        for path in (".env.example", "installer/installer.py", "frontend/index.html",
                     "frontend/server-settings.js", "frontend/app.js", "frontend/job.js", "frontend/job.html"):
            src = (ROOT / path).read_text(encoding="utf-8")
            for gone in ("TFNSW", "TOMTOM", "TomTom", "Transport for NSW", "OSRM", "/api/commute/status",
                         "/api/settings/keys", "commute-keys", "Commute data", "Arrive at work by"):
                self.assertNotIn(gone, src, f"{path}: {gone}")
        # The README names the old services only to say they are gone (Updating).
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        note = readme[readme.index("**Commute links instead of commute times.**"):]
        note = note[:note.index("\n\n**")]
        for gone in ("TFNSW", "TOMTOM", "TomTom", "Transport for NSW", "OSRM"):
            self.assertEqual(readme.count(gone), note.count(gone), gone)
        for gone in ("## Commute times", "Commute data", "arriving by 9:00", "/api/commute/status"):
            self.assertNotIn(gone, readme)

    def test_the_commute_cache_table_is_not_made_any_more(self):
        with engine.connect() as conn:
            names = {n for (n,) in conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'table'"))}
        self.assertNotIn("commute_cache", names)
        self.assertNotIn("commute_cache", {t.name for t in __import__("backend.db").db.Base.metadata.sorted_tables})


class OfficesTest(Workspaced):
    OFFICES = [{"name": "Sydney office", "address": "1 Denison St, North Sydney NSW 2060", "url": "https://acme.example/a"},
               {"name": "Head office", "address": "100 Collins St, Melbourne VIC 3000", "url": "https://acme.example/b"},
               {"name": "", "address": "5 Smith St, Parramatta NSW 2150", "url": "https://acme.example/c"}]
    PLACES = {"1 Denison St, North Sydney NSW 2060": (-33.839, 151.207), "5 Smith St, Parramatta NSW 2150": (-33.815, 151.0),
              "100 Collins St, Melbourne VIC 3000": (-37.81, 144.97), "Sydney NSW": (-33.87, 151.21)}

    def setUp(self):
        super().setUp()
        db = SessionLocal()
        try:
            row = db.get(CompanyProfile, "acme") or CompanyProfile(key="acme", name="Acme", status="done")
            row.offices_json = json.dumps(self.OFFICES)
            db.merge(row)
            db.commit()
        finally:
            db.close()

    def geocode(self, query):
        hit = self.PLACES.get(query)
        return {"lat": hit[0], "lng": hit[1], "country": "AU", "type": "building"} if hit else None

    def test_every_known_office_is_listed_and_any_can_be_picked(self):
        job = self.add_job(location_text="Sydney NSW")
        with helpers.client() as c, mock.patch("backend.offices.geocode", side_effect=self.geocode), \
                mock.patch("backend.pipeline.geocode", side_effect=self.geocode, create=True):
            listed = c.get(f"/api/jobs/{job}", headers=self.h).json()["office_candidates"]
            self.assertEqual(sorted(o["address"] for o in listed), sorted(o["address"] for o in self.OFFICES))
            near = {o["address"]: o["near"] for o in listed}
            self.assertFalse(near["100 Collins St, Melbourne VIC 3000"])  # far from the ad's city, still listed
            self.assertEqual(listed[-1]["address"], "100 Collins St, Melbourne VIC 3000")
            # The head office in another city can be picked: it is one of the employer's own.
            r = c.put(f"/api/jobs/{job}/office", headers=self.h, json={"office": "100 Collins St, Melbourne VIC 3000"})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual((r.json()["office_text"], r.json()["office_source"]),
                             ("100 Collins St, Melbourne VIC 3000", "user"))
            self.assertEqual(len(r.json()["office_candidates"]), 3, "all offices stay listed after the pick")
            out = c.get(f"/api/jobs/{job}/commute", headers=self.h).json()
            self.assertEqual(out["destination"], "100 Collins St, Melbourne VIC 3000")
            # A typed place far from the ad's city is still refused.
            r = c.put(f"/api/jobs/{job}/office", headers=self.h, json={"office": "Sydney NSW, but in Melbourne"})
            self.assertEqual(r.status_code, 422)


@unittest.skipUnless(shutil.which("node"), "needs node to run the page's script")
class LinkBuildingTest(unittest.TestCase):
    """job.js builds each Google Maps link itself, with encodeURIComponent: names with commas, ampersands, quotes,
    hashes and non-Latin letters arrive in Google as they were typed."""

    @classmethod
    def setUpClass(cls):
        cls.src = (FRONTEND / "job.js").read_text(encoding="utf-8")
        m = re.search(r"function mapsDirectionsUrl\(origin, destination, mode\) \{.*?\n\}\n", cls.src, re.S)
        assert m, "mapsDirectionsUrl() not found in job.js"
        cls.fn = m.group(0)

    def url(self, origin, destination, mode):
        script = f"{self.fn}\nconsole.log(mapsDirectionsUrl({json.dumps(origin)}, {json.dumps(destination)}, " \
                 f"{json.dumps(mode)}));"
        return subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True,
                              encoding="utf-8").stdout.strip()

    def test_names_come_through_whole(self):
        cases = [("Café & Co, 1 Rue de l'Église", "Level 5, 1 Denison St, North Sydney NSW 2060", "driving"),
                 ("東京駅", "Москва, Красная площадь & Co", "transit"),
                 ("St Mary's #3 / 50% off? a=b&c=d+e", "\"Quoted\" Pty Ltd, Ōtautahi", "transit"),
                 ("-33.425000,151.341700", "Sydney NSW", "driving")]
        for origin, destination, mode in cases:
            with self.subTest(origin=origin):
                url = self.url(origin, destination, mode)
                parts = urlsplit(url)
                self.assertEqual((parts.scheme, parts.netloc, parts.path), ("https", "www.google.com", "/maps/dir/"))
                self.assertTrue(url.isascii(), url)
                self.assertNotIn(" ", url)
                query = parse_qs(parts.query, strict_parsing=True)
                self.assertEqual(query, {"api": ["1"], "origin": [origin], "destination": [destination],
                                         "travelmode": [mode]})
                self.assertEqual(parts.query.count("&"), 3, "an & in a name must not start another parameter")

    def test_only_the_two_travel_modes(self):
        self.assertTrue(self.url("a", "b", "walking").endswith("&travelmode=driving"))

    def test_links_open_in_a_new_tab_without_telling_google_where_from(self):
        self.assertIn("a.target = '_blank';", self.src)
        self.assertIn("a.rel = 'noopener noreferrer';", self.src)
        self.assertIn("a.href = mapsDirectionsUrl(", self.src)
        self.assertIn("Opening a link shows Google both places", self.src)
        self.assertNotIn("innerHTML = `<a href=\"${esc(c.", self.src)

    def test_the_job_page_has_no_route_map_or_times(self):
        page = (FRONTEND / "job.html").read_text(encoding="utf-8")
        self.assertNotIn("commute-map", page)
        self.assertNotIn("leaflet", page)
        for gone in ("drawMap", "journeyHtml", "carHtml", "arrive_by", "L.map("):
            self.assertNotIn(gone, self.src)


class ExportTest(Workspaced):
    def test_search_setup_shows_starting_points_instead_of_commute_settings(self):
        with helpers.client() as c:
            home = c.post("/api/pins", headers=self.h, json={"label": "Home | flat", "kind": "home", "lat": -33.4,
                                                             "lng": 151.3}).json()["id"]
            hybrid = c.post("/api/pins", headers=self.h, json={"label": "Mum", "kind": "hybrid", "lat": -33.6,
                                                               "lng": 151.1}).json()["id"]
            self.put_points(c, [{"label": "Station", "place": "Gosford Station NSW", "use": "transit"},
                                {"label": "", "pin_id": hybrid, "use": "driving"}])
            self.assertIsNotNone(home)
        with helpers.as_admin():
            db = SessionLocal()
            try:
                md = export._setup_md(db, db.get(Workspace, self.ws))
            finally:
                db.close()
        section = md[md.index("## Starting points"):md.index("## Search schedule")]
        self.assertIn("| Label | Place | Used for |", section)
        self.assertIn("| Home \\| flat | home pin (-33.4, 151.3) | driving and public transport |", section)
        self.assertIn("| Station | Gosford Station NSW | public transport |", section)
        self.assertIn("| Mum | pin Mum (-33.6, 151.1) | driving |", section)
        for gone in ("## Commute", "Arrive at work by", "Leave work at", "Public transport from"):
            self.assertNotIn(gone, md)

    def test_an_old_search_setup_exports_its_stations_as_starting_points(self):
        with helpers.as_admin():
            db = SessionLocal()
            db.add(UserProfile(id=self.ws, titles=["Analyst"], commute_from=["Central Station"],
                               commute_arrive_by="08:30", commute_leave_at="17:30"))
            db.commit()
            md = export._setup_md(db, db.get(Workspace, self.ws))
            db.close()
        self.assertIn("| Central Station | Central Station | public transport |", md)
        self.assertNotIn("08:30", md)

    def test_nothing_set_says_none(self):
        with helpers.as_admin():
            db = SessionLocal()
            md = export._setup_md(db, db.get(Workspace, self.ws))
            db.close()
        self.assertIn("## Starting points\n\nNone.\n", md)


class LegacyHelpersTest(unittest.TestCase):
    def test_stations_become_public_transport_starting_points(self):
        self.assertEqual(commute.legacy_starting_points(["A", " a ", "", "B\nStation"]), [
            {"label": "A", "pin_id": None, "place": "A", "use": "transit"},
            {"label": "B Station", "pin_id": None, "place": "B Station", "use": "transit"}])

    def test_malformed_stored_points_are_dropped(self):
        self.assertEqual(commute.clean_starting_points([None, "x", {"use": "both"}, {"pin_id": True, "use": "both"},
                                                        {"pin_id": 3, "place": "p", "use": "nope"}]),
                         [{"label": "", "pin_id": 3, "place": None, "use": "both"}])


if __name__ == "__main__":
    unittest.main()
