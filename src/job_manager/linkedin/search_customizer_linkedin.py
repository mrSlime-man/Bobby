"""
This module is used to customize the search parameters for the LinkedIn jobs search.
"""

import argparse
import asyncio
import json
import os
import re
from inspect import isawaitable
from pathlib import Path
from typing import Any, Union
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from playwright.sync_api import Page

from config.app_config import EASY_APPLY_ONLY_MODE
from config.constants import SEARCH_CONFIG_FILE

try:
    from config.app_config import LINKEDIN_RECOMMENDED_JOBS_MODE
except ImportError:
    LINKEDIN_RECOMMENDED_JOBS_MODE = False
try:
    from config.app_config import LINKEDIN_TOP_APPLICANT_JOBS_MODE
except ImportError:
    LINKEDIN_TOP_APPLICANT_JOBS_MODE = False
from config.logger_config import logger

# Import Playwright utilities for enhanced functionality
from src.job_manager.search_customizer import BaseSearchCustomizer
from src.utils.browser_utils import (
    find_element_safely,
    find_elements_safely,
    get_clean_text,
    safe_click,
    safe_fill,
)
from src.utils.runtime_control import runtime_controller
from src.utils.utils import async_pause, load_yaml_file


class SearchCustomizer(BaseSearchCustomizer):
    RECOMMENDED_JOBS_URL = "https://www.linkedin.com/jobs/collections/recommended/"
    TOP_APPLICANT_JOBS_URL = "https://www.linkedin.com/jobs/collections/top-applicant/"

    # LinkedIn's current search URL values, corroborated by Bobby's local
    # production logs. URL state is substantially less brittle than the
    # frequently changing All Filters modal and also replaces stale state.
    WORK_TYPE_CODES = {"onsite": "1", "remote": "2", "hybrid": "3"}
    EXPERIENCE_CODES = {
        "internship": "1",
        "entry": "2",
        "associate": "3",
        "mid_senior_level": "4",
        "director": "5",
        "executive": "6",
    }
    JOB_TYPE_CODES = {
        "full_time": "F",
        "contract": "C",
        "part_time": "P",
        "temporary": "T",
        "volunteer": "V",
        "internship": "I",
        "other": "O",
    }
    DATE_CODES = {
        # Pydantic serializes the accepted YAML alias `24_hours` under this
        # field name.  Retain the legacy spelling as well for direct callers
        # and older in-memory configurations.
        "day_24_hours": "r86400",
        "24_hours": "r86400",
        "week": "r604800",
        "month": "r2592000",
    }
    MANAGED_URL_PARAMETERS = {
        "f_AL",
        "f_E",
        "f_JT",
        "f_TPR",
        "f_WT",
        "geoId",
        "keywords",
        "location",
        "start",
        "currentJobId",
    }
    SEARCH_ROTATION_PATH = (
        Path(__file__).resolve().parents[3] / "data/output/linkedin/search_rotation.json"
    )

    def __init__(self, page: Union[Page, Any]):
        super().__init__(page)
        self.active_profile = "custom"
        self.active_query_family = "default"
        self.active_query_index = 0
        self.active_location_index = 0
        logger.info("SearchCustomizer initialized")

    @staticmethod
    def _deduplicated_terms(values: object) -> list[str]:
        terms: list[str] = []
        seen: set[str] = set()
        for value in values if isinstance(values, (list, tuple)) else ():
            term = str(value or "").strip()
            normalized = term.casefold()
            if term and normalized not in seen:
                seen.add(normalized)
                terms.append(term)
        return terms

    @classmethod
    def _load_rotation_cursors(cls) -> dict[str, int]:
        try:
            payload = json.loads(cls.SEARCH_ROTATION_PATH.read_text(encoding="utf-8"))
            cursors = payload.get("cursors", {}) if isinstance(payload, dict) else {}
            return {
                str(key): int(value)
                for key, value in cursors.items()
                if isinstance(value, int) and value >= 0
            }
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            logger.warning("SEARCH_ROTATION_STATE_INVALID | starting from first configured family")
            return {}

    @classmethod
    def _save_rotation_cursors(cls, cursors: dict[str, int]) -> None:
        path = cls.SEARCH_ROTATION_PATH
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps({"version": 1, "cursors": cursors}, sort_keys=True),
                encoding="utf-8",
            )
            temporary.chmod(0o600)
            os.replace(temporary, path)
            path.chmod(0o600)
        except OSError as error:
            logger.warning(
                "SEARCH_ROTATION_STATE_WRITE_FAILED | "
                f"error_class={type(error).__name__}"
            )
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    @classmethod
    def _next_rotation_index(cls, key: str, size: int) -> int:
        if size <= 1:
            return 0
        cursors = cls._load_rotation_cursors()
        index = cursors.get(key, 0) % size
        cursors[key] = (index + 1) % size
        cls._save_rotation_cursors(cursors)
        return index

    def set_advanced_search_params(self, parameters: dict[str, Any]) -> None:
        """Select one bounded query/location slice before setting LinkedIn filters."""
        super().set_advanced_search_params(parameters)
        self.active_profile = str(parameters.get("search_profile") or self._profile_name())

        raw_families = parameters.get("query_families") or []
        families: list[tuple[str, list[str]]] = []
        for index, raw_family in enumerate(raw_families):
            if not isinstance(raw_family, dict):
                continue
            terms = self._deduplicated_terms(raw_family.get("positions"))
            if terms:
                name = str(raw_family.get("name") or f"family_{index + 1}").strip()
                families.append((name or f"family_{index + 1}", terms))
        if not families:
            families = [("default", self._deduplicated_terms(self.positions))]
        families = [family for family in families if family[1]]
        if families:
            self.active_query_index = self._next_rotation_index(
                f"{self.active_profile}:query", len(families)
            )
            self.active_query_family, self.positions = families[self.active_query_index]

        location_variants = self._deduplicated_terms(
            parameters.get("location_variants") or self.locations
        )
        if location_variants:
            self.active_location_index = self._next_rotation_index(
                f"{self.active_profile}:location", len(location_variants)
            )
            self.locations = [location_variants[self.active_location_index]]

        logger.info(
            "SEARCH_QUERY_SELECTED | "
            f"profile={self.active_profile} | family={self.active_query_family} | "
            f"query_index={self.active_query_index + 1}/{len(families)} | "
            f"term_count={len(self.positions)} | "
            f"location_index={self.active_location_index + 1}/{max(1, len(location_variants))}"
        )

    @staticmethod
    def _enabled_codes(values: dict[str, Any], mapping: dict[str, str]) -> list[str]:
        return [code for key, code in mapping.items() if values.get(key) is True]

    def _profile_name(self) -> str:
        if self.active_profile in {"remote", "tampa"}:
            return self.active_profile
        location = " ".join(str(value) for value in self.locations).casefold()
        if self.remote and not self.hybrid and not self.onsite and "united states" in location:
            return "remote"
        if "tampa" in location:
            return "tampa"
        return "custom"

    def _expected_url_parameters(self) -> dict[str, str]:
        expected = {
            "keywords": self.format_linkedin_keyword_query(),
            "location": ", ".join(self.locations),
        }
        work_types = self._enabled_codes(
            {"remote": self.remote, "hybrid": self.hybrid, "onsite": self.onsite},
            self.WORK_TYPE_CODES,
        )
        experience = self._enabled_codes(self.experience_level, self.EXPERIENCE_CODES)
        job_types = self._enabled_codes(self.job_types, self.JOB_TYPE_CODES)
        date_codes = self._enabled_codes(self.date_posted, self.DATE_CODES)
        if work_types:
            expected["f_WT"] = ",".join(work_types)
        if experience:
            expected["f_E"] = ",".join(experience)
        if job_types:
            expected["f_JT"] = ",".join(job_types)
        if date_codes:
            expected["f_TPR"] = date_codes[0]
        if EASY_APPLY_ONLY_MODE:
            expected["f_AL"] = "true"
        return expected

    def build_profile_search_url(self, current_url: str | None = None) -> str:
        """Start a fresh search, including removal of unconfigured sticky filters."""
        query = list(self._expected_url_parameters().items())
        query.append(("refresh", "true"))
        return urlunsplit(("https", "www.linkedin.com", "/jobs/search/", urlencode(query), ""))

    @staticmethod
    def _split_codes(value: str) -> set[str]:
        return {part.strip() for part in str(value).split(",") if part.strip()}

    def _url_state_matches_profile(self, current_url: str) -> tuple[bool, list[str]]:
        parts = urlsplit(current_url)
        pairs = parse_qsl(parts.query, keep_blank_values=True)
        actual = dict(pairs)
        expected = self._expected_url_parameters()
        mismatches: list[str] = []
        if parts.hostname not in {"linkedin.com", "www.linkedin.com"} or parts.path.rstrip("/") != "/jobs/search":
            mismatches.append("search_page")
        for key, _ in pairs:
            if key.startswith("f_") and key not in expected:
                mismatches.append(key)
            if key in self.MANAGED_URL_PARAMETERS and sum(k == key for k, _ in pairs) > 1:
                mismatches.append(key)
        set_parameters = {"f_WT", "f_E", "f_JT"}
        for key in self.MANAGED_URL_PARAMETERS - {"geoId", "start", "currentJobId"}:
            expected_value = expected.get(key)
            actual_value = actual.get(key)
            if key in set_parameters:
                matches = self._split_codes(actual_value or "") == self._split_codes(
                    expected_value or ""
                )
            elif key in {"keywords", "location"}:
                matches = (actual_value or "").strip().casefold() == (
                    expected_value or ""
                ).strip().casefold()
            elif key == "f_AL":
                actual_enabled = str(actual_value or "").strip().casefold() in {
                    "true",
                    "1",
                    "yes",
                }
                matches = actual_enabled if expected_value == "true" else key not in actual
            else:
                matches = (actual_value or "") == (expected_value or "")
            if not matches:
                mismatches.append(key)
        return not mismatches, sorted(set(mismatches))

    # Inspect only search controls; never return page text or application values.
    # Native checked state also supports LinkedIn's visually hidden checkbox inputs.
    FILTER_UI_SNAPSHOT = r"""() => {
        const visible = el => !!el && el.getClientRects().length > 0 &&
            getComputedStyle(el).visibility !== 'hidden';
        const norm = value => (value || '').replace(/\s+/g, ' ').trim().toLowerCase();
        const dialogs = [...document.querySelectorAll('[role="dialog"], .artdeco-modal')];
        const dialog = dialogs.find(el => visible(el) && /all filters/i.test(el.innerText));
        if (!dialog) return {dialog: false};
        const result = {dialog: true, f_WT: {}, f_E: {}, f_JT: {}, f_TPR: {}, easy_apply: null};
        const labels = {
            f_WT: {'on-site': '1', 'on site': '1', 'remote': '2', 'hybrid': '3'},
            f_E: {'internship': '1', 'entry level': '2', 'associate': '3',
                  'mid-senior level': '4', 'director': '5', 'executive': '6'},
            f_JT: {'full-time': 'F', 'contract': 'C', 'part-time': 'P',
                   'temporary': 'T', 'volunteer': 'V', 'internship': 'I', 'other': 'O'},
            f_TPR: {'any time': '', 'past month': 'r2592000', 'past week': 'r604800',
                    'past 24 hours': 'r86400'}
        };
        const groups = {
            f_WT: /workplace|work.?type|work.?location|on.?site.*remote/,
            f_E: /experience.?level/,
            f_JT: /job.?type/,
            f_TPR: /date.?posted|time.?posted/
        };
        const checked = el => {
            const aria = el.getAttribute('aria-checked') ?? el.getAttribute('aria-pressed');
            if (aria === 'true' || aria === 'false') return aria === 'true';
            if (el instanceof HTMLInputElement && ['checkbox', 'radio'].includes(el.type)) return el.checked;
            return null;
        };
        const controls = [...dialog.querySelectorAll('input[type="checkbox"], input[type="radio"], [role="checkbox"], [role="radio"], [role="switch"]')];
        for (const el of controls) {
            const associated = [...(el.labels || [])];
            const namedBy = (el.getAttribute('aria-labelledby') || '').split(/\s+/)
                .map(id => document.getElementById(id)).filter(Boolean);
            const names = [...associated, ...namedBy].map(label => norm(label.textContent));
            names.push(norm(el.getAttribute('aria-label')));
            const metadata = norm([el.id, el.name, el.getAttribute('data-test-work-location-filter'),
                el.getAttribute('data-test-experience-level-filter'), el.getAttribute('data-test-job-type-filter')].join(' '));
            let group = Object.keys(groups).find(key => groups[key].test(metadata));
            let section = el.parentElement;
            let heading = '';
            while (section && section !== dialog) {
                const titles = [...section.querySelectorAll('h2,h3,h4,legend')];
                if (titles.length === 1) {
                    heading = norm(titles[0].textContent);
                    if (!group) group = Object.keys(groups).find(key => groups[key].test(heading));
                    if (group || /easy apply/.test(heading)) break;
                }
                section = section.parentElement;
            }
            if (!visible(el) && !associated.some(visible) && !namedBy.some(visible) && !visible(el.parentElement)) continue;
            if (/easy.?apply/.test(metadata) || names.some(name => /easy apply/.test(name)) || /easy apply/.test(heading)) {
                const state = checked(el);
                if (result.easy_apply !== null && result.easy_apply !== state) result.easy_apply = 'conflict';
                else result.easy_apply = state;
                continue;
            }
            // Unique labels remain usable when LinkedIn changes its wrapper classes.
            // The duplicated Internship option must have a semantic group.
            if (!group) {
                const candidates = Object.keys(labels).filter(key => names.some(name => name in labels[key]));
                if (candidates.length === 1) group = candidates[0];
            }
            if (!group) continue;
            const label = names.find(name => name in labels[group]);
            // LinkedIn's "Any time" radio currently uses value="on" and an
            // id ending in a bare hyphen, while the other date radios use the
            // public r* values. Normalize that placeholder to the canonical
            // empty f_TPR value.
            const value = group === 'f_TPR' && /timePostedRange-$/.test(el.id)
                ? '' : (label !== undefined ? labels[group][label] : el.value);
            if (!Object.values(labels[group]).includes(value)) continue;
            const state = checked(el);
            if (value in result[group] && result[group][value] !== state) result[group][value] = 'conflict';
            else result[group][value] = state;
        }
        const locationInputs = [...document.querySelectorAll(
            'input[id*="jobs-search-box-location"], input[aria-label*="City, state"], input[aria-label*="or zip code"], input[aria-label*="location" i]'
        )].filter(visible);
        result.locations = [...new Set(locationInputs.map(el => el.value.trim()))];
        return result;
    }"""

    def _ui_state_matches_profile(self, snapshot: dict[str, Any]) -> tuple[bool, list[str]]:
        if not isinstance(snapshot, dict) or snapshot.get("dialog") is not True:
            return False, ["ui_unavailable"]
        mismatches = []
        expected = self._expected_url_parameters()
        for parameter, codes in (
            ("f_WT", self.WORK_TYPE_CODES.values()),
            ("f_E", self.EXPERIENCE_CODES.values()),
            ("f_JT", self.JOB_TYPE_CODES.values()),
            ("f_TPR", ["", *self.DATE_CODES.values()]),
        ):
            selected = self._split_codes(expected.get(parameter, ""))
            if parameter == "f_TPR" and not selected:
                selected = {""}
            states = snapshot.get(parameter, {})
            # Only compare controls LinkedIn actually rendered. Some account
            # variants omit legacy job-type options (temporary, volunteer,
            # etc.); an absent unselected option is not a stale selection.
            # Every option selected by this profile must still be present and
            # checked, and every rendered unselected option must be unchecked.
            if not isinstance(states, dict) or not states or any(
                states.get(code) is not True for code in selected
            ) or any(
                state is not False
                for code, state in states.items()
                if code in set(codes) and code not in selected
            ):
                mismatches.append(f"ui_{parameter}")
        if snapshot.get("easy_apply") is not EASY_APPLY_ONLY_MODE:
            mismatches.append("ui_f_AL")
        intended_location = ", ".join(self.locations).strip().casefold()
        # LinkedIn resolves metropolitan-city shorthand to a state/country
        # label. Accept only that selected city and its canonical geographic
        # suffixes; rotation must never inherit a nearby city's sticky
        # location. This applies to custom locations as well as the Tampa
        # profile because LinkedIn uses the same rendering there.
        accepted_locations = {intended_location}
        if intended_location:
            accepted_locations.update(
                {
                    f"{intended_location}, fl",
                    f"{intended_location}, florida",
                    f"{intended_location}, florida, united states",
                    f"{intended_location}, fl, united states",
                }
            )
        # LinkedIn may omit punctuation in a selected city (for example,
        # ``St. Petersburg`` becomes ``St Petersburg, FL``). Normalize only
        # punctuation after enumerating the exact selected city and allowed
        # Florida suffixes; this is deliberately not fuzzy geographic matching
        # and cannot admit a nearby/stale Tampa-area location.
        def canonical_location(value: object) -> str:
            return re.sub(r"[^a-z0-9]+", " ", str(value).casefold()).strip()

        accepted_locations = {
            canonical_location(location) for location in accepted_locations if canonical_location(location)
        }
        # LinkedIn renders an empty proxy input alongside the populated
        # location control. Ignore empty proxies, but require one non-empty
        # location to match the selected profile.
        observed = [
            canonical_location(value)
            for value in snapshot.get("locations", [])
            if canonical_location(value)
        ]
        if not observed or any(value not in accepted_locations for value in observed):
            mismatches.append("ui_location")
        return not mismatches, mismatches

    async def _verify_filter_ui(self) -> tuple[bool, list[str], Any]:
        """Open All Filters read-only, inspect actual selections, then dismiss it."""
        opened = False
        snapshot = {}
        try:
            if runtime_controller.is_shutdown_requested():
                return False, ["shutdown"], None
            async with asyncio.timeout(18):
                opened = await self._open_all_filters()
                if not opened or runtime_controller.is_shutdown_requested():
                    return False, ["ui_unavailable"], None
                # Modal animation/hydration is independent of the URL load event.
                # On a cold profile LinkedIn can populate the advanced controls
                # several seconds after the dialog itself becomes clickable.
                # Keep this bounded and poll only the allow-listed control
                # snapshot; never wait for or capture page/application text.
                for _ in range(12):
                    snapshot = await self.page.evaluate(self.FILTER_UI_SNAPSHOT)
                    verified, mismatches = self._ui_state_matches_profile(snapshot)
                    if verified or runtime_controller.is_shutdown_requested():
                        break
                    await asyncio.sleep(0.5)
                return verified, mismatches, snapshot.get("easy_apply") if isinstance(snapshot, dict) else None
        except Exception as error:
            logger.warning(f"FILTER_UI_READ_FAILED | error_type={type(error).__name__}")
            return False, ["ui_unavailable"], None
        finally:
            if opened and not runtime_controller.is_shutdown_requested():
                try:
                    await self.page.keyboard.press("Escape", timeout=2000)
                except Exception:
                    self.filters_verified = False

    async def verify_current_search_state(self, *, log_result: bool = True) -> bool:
        """Require both the refreshed search URL and independent UI selections."""
        self.filters_verified = False
        if runtime_controller.is_shutdown_requested():
            return False
        current_url = str(getattr(self.page, "url", "") or "")
        url_verified, mismatches = self._url_state_matches_profile(current_url)
        ui_verified, ui_mismatches, easy_ui = False, [], None
        if url_verified:
            ui_verified, ui_mismatches, easy_ui = await self._verify_filter_ui()
            # Detect redirects or UI changes occurring while the modal was open.
            url_verified, final_mismatches = self._url_state_matches_profile(str(getattr(self.page, "url", "") or ""))
            mismatches.extend(final_mismatches)
        mismatches.extend(ui_mismatches)
        verified = url_verified and ui_verified and not runtime_controller.is_shutdown_requested()
        self.filters_verified = verified
        if log_result or not verified:
            logger.info(
                "SEARCH_PROFILE_AUDIT | "
                f"profile={self._profile_name()} | "
                f"location={','.join(map(str, self.locations)) or 'none'} | "
                f"remote={str(self.remote).lower()} | "
                f"hybrid={str(self.hybrid).lower()} | "
                f"onsite={str(self.onsite).lower()} | "
                f"easy_apply={str(EASY_APPLY_ONLY_MODE).lower()} | "
                f"url_verified={str(url_verified).lower()} | ui_verified={str(ui_verified).lower()} | "
                f"verified={str(verified).lower()} | "
                f"mismatches={','.join(sorted(set(mismatches))) or 'none'}"
            )
            logger.info(
                f"POST-FILTER EASY APPLY AUDIT | config={EASY_APPLY_ONLY_MODE} | ui={easy_ui} | "
                f"url_f_AL={'f_AL' in dict(parse_qsl(urlsplit(str(getattr(self.page, 'url', '') or '')).query))} | "
                f"pass={verified}"
            )
        return verified

    async def _establish_and_verify_profile_filters(self) -> bool:
        """Replace managed URL filters and verify, with one bounded repair."""
        self.filters_verified = False
        for attempt in (1, 2):
            if runtime_controller.is_shutdown_requested():
                return False
            target_url = self.build_profile_search_url()
            logger.info(
                "Establishing deterministic LinkedIn profile filters | "
                f"profile={self._profile_name()} | attempt={attempt}"
            )
            try:
                await self.page.goto(target_url, wait_until="domcontentloaded", timeout=20000)
                await async_pause(2, 3)
                verified = await self.verify_current_search_state(log_result=True)
            except Exception as error:
                logger.warning(f"FILTER_APPLY_FAILED | error_type={type(error).__name__}")
                verified = False
            if verified:
                logger.info(
                    f"FILTER_GATE_PASS | profile={self._profile_name()} | attempt={attempt} | state=JOB_INTAKE_ALLOWED"
                )
                return True
            logger.warning(
                "FILTER_GATE_REPAIR | "
                f"profile={self._profile_name()} | attempt={attempt}"
            )
        logger.error(f"FILTER_GATE_FAILED | profile={self._profile_name()}")
        return False

    async def _remove_easy_apply_url_filter(self) -> bool:
        """Remove only f_AL while preserving every other search parameter."""
        if EASY_APPLY_ONLY_MODE:
            return False
        current_url = str(getattr(self.page, "url", "") or "")
        parts = urlsplit(current_url)
        query_pairs = [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key != "f_AL"
        ]
        clean_url = urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(query_pairs, doseq=True), parts.fragment)
        )
        if clean_url and clean_url != current_url:
            logger.info("Removing persisted f_AL before search results request")
            await self.page.goto(clean_url, wait_until="domcontentloaded")
            await async_pause(1, 1)
            return True
        return False

    async def _open_recommended_jobs(self) -> None:
        """Navigate to LinkedIn recommended jobs and skip configured position keywords."""
        logger.info("LinkedIn recommended jobs mode enabled; ignoring configured positions")
        await self.page.goto(self.RECOMMENDED_JOBS_URL, wait_until="domcontentloaded")
        await async_pause(2, 3)

    async def _open_top_applicant_jobs(self) -> None:
        """Navigate to LinkedIn Top applicant picks and skip configured position keywords."""
        logger.info("LinkedIn top applicant jobs mode enabled; ignoring configured positions")
        await self.page.goto(self.TOP_APPLICANT_JOBS_URL, wait_until="domcontentloaded")
        await async_pause(2, 3)

    def format_linkedin_keyword_query(self) -> str:
        """Format positions as a LinkedIn boolean keyword query."""
        cleaned_positions = [
            position.strip() for position in self.positions if position and position.strip()
        ]
        return " OR ".join(f'"{position}"' for position in cleaned_positions)

    async def _set_basic_search_terms(self):
        """Set basic search parameters (keywords and location) - async"""
        try:
            # Set job title/keywords
            if self.positions:
                keyword_query = self.format_linkedin_keyword_query()
                keyword_selectors = [
                    "input[aria-label*='or company']:not([disabled]):not([aria-hidden='true'])",
                    "input[aria-label*='Search by title']:not([disabled]):not([aria-hidden='true'])",
                    "#jobs-search-box-keyword-id-ember:not([disabled])",
                    ".jobs-search-box__input--keyword:not([disabled])",
                    "input[role='combobox'][aria-label*='Search by title']:not([disabled])",
                ]

                keywords_filled = False
                for selector in keyword_selectors:
                    if await safe_fill(self.page, selector, keyword_query, wait_for_timeout=2000):
                        logger.info(f"Keywords set: {keyword_query}")
                        keywords_filled = True
                        # await async_pause(1, 2)
                        break

                if not keywords_filled:
                    logger.warning("Could not find or fill keywords field")

            location_selectors = [
                "input[aria-label*='or zip code']:not([disabled]):not([aria-hidden='true'])",
                "input[aria-label*='City, state']:not([disabled]):not([aria-hidden='true'])",
                "#jobs-search-box-location-id-ember:not([disabled])",
                "input[id^='jobs-search-box-location-id-ember']:not([disabled])",
                ".jobs-search-box__input--location:not([disabled])",
                "input[aria-label*='location']:not([disabled]):not([aria-hidden='true'])",
            ]

            # Set or clear location. LinkedIn often keeps a previous/default location in this field.
            if self.locations:
                location_filled = False
                for selector in location_selectors:
                    if await safe_fill(
                        self.page, selector, ", ".join(self.locations), wait_for_timeout=2000
                    ):
                        logger.info(f"Location set: {', '.join(self.locations)}")
                        # await async_pause()

                        # Try to press Enter to apply location
                        element = await find_element_safely(self.page, selector)
                        if element:
                            await self.page.keyboard.press("Enter")

                        location_filled = True
                        break

                if not location_filled:
                    logger.warning("Could not find or fill location field")

            else:
                location_cleared = False
                for selector in location_selectors:
                    if await safe_fill(self.page, selector, "", wait_for_timeout=2000):
                        logger.info(
                            "Location search field cleared because no locations are configured"
                        )
                        await self._dismiss_location_typeahead()
                        location_cleared = True
                        break

                if not location_cleared:
                    logger.warning("Could not find or clear location field")

                # await async_pause()

        except Exception as e:
            logger.error(f"Error setting basic search parameters: {e}")

    async def _commit_basic_search(self) -> None:
        """Submit keyword/location fields before filters so LinkedIn preserves them."""
        search_selectors = [
            "button.jobs-search-box__submit-button",
            "button[aria-label='Search']",
            "button:has-text('Search')",
        ]
        for selector in search_selectors:
            if await safe_click(self.page, selector):
                logger.info("Basic LinkedIn search submitted before applying filters")
                await async_pause(2, 3)
                return

        logger.warning("Could not click Search button; pressing Enter to submit basic search")
        try:
            await self.page.keyboard.press("Enter")
            await async_pause(2, 3)
        except Exception as e:
            logger.warning(f"Could not submit basic search with Enter: {e}")

    async def _dismiss_location_typeahead(self) -> None:
        """Close LinkedIn's location suggestions so filter buttons are clickable."""
        try:
            await self.page.keyboard.press("Escape")
            await async_pause(1, 1)
        except Exception as e:
            logger.debug(f"Failed pressing Escape to close location typeahead: {e}")

        try:
            await self.page.evaluate("document.activeElement && document.activeElement.blur()")
        except Exception as e:
            logger.debug(f"Failed blurring active location field: {e}")

    async def _open_all_filters(self):
        """Open 'All filters' modal window (async)"""
        try:
            filters_selectors = [
                "//button[contains(., 'All filters')]",
                "button[aria-label*='All filters']",
                "button[aria-label*='Show all filters']",
                ".jobs-search-results-list__filter-button[aria-label*='filters']",
            ]

            for selector in filters_selectors:
                if await safe_click(self.page, selector):
                    # await async_pause()
                    logger.info("Filters modal window opened")
                    return True

            logger.warning("Could not find or click All filters button")
            return False
        except Exception as e:
            logger.error(f"Failed to open filters: {e}")
        return False

    async def _set_date_posted_filter(self):
        """Set date posted filter (async)"""
        if not self.date_posted:
            return

        try:
            date_mapping = {
                "day_24_hours": "Past 24 hours",
                "24_hours": "Past 24 hours",
                "week": "Past week",
                "month": "Past month",
                "all_time": "Any time",
            }

            for date_key, is_enabled in self.date_posted.items():
                if is_enabled and date_key in date_mapping:
                    date_text = date_mapping[date_key]

                    # Try multiple selector approaches
                    date_selectors = [
                        f"//label[contains(., '{date_text}')]",
                        f"//input[@value='{date_text}']/..",
                        f"label:has-text('{date_text}')",
                        f"[data-test-date-posted-filter-option='{date_key}']",
                    ]

                    date_set = False
                    for selector in date_selectors:
                        if await safe_click(self.page, selector):
                            logger.info(f"Date filter set: {date_text}")
                            date_set = True
                            break

                    if not date_set:
                        logger.warning(f"Could not set date filter: {date_text}")

                    # await async_pause()
                    break

        except Exception as e:
            logger.error(f"Error setting date filter: {e}")

    async def _set_experience_level_filter(self):
        """Set experience level filter (async)"""
        if not self.experience_level:
            return

        try:
            experience_mapping = {
                "internship": "Internship",
                "entry": "Entry level",
                "associate": "Associate",
                "mid_senior_level": "Mid-Senior level",
                "director": "Director",
                "executive": "Executive",
            }

            for exp_key, is_enabled in self.experience_level.items():
                if is_enabled and exp_key in experience_mapping:
                    exp_text = experience_mapping[exp_key]

                    # Try multiple selector approaches
                    exp_selectors = [
                        f"//label[contains(., '{exp_text}')]",
                        f"//input[@value='{exp_text}']/..",
                        f"label:has-text('{exp_text}')",
                        f"[data-test-experience-level-filter='{exp_key}']",
                    ]

                    exp_set = False
                    for selector in exp_selectors:
                        if await safe_click(self.page, selector):
                            logger.info(f"Experience level set: {exp_text}")
                            exp_set = True
                            break

                    if not exp_set:
                        logger.warning(f"Element not found for experience level: {exp_text}")

                    # await async_pause()

        except Exception as e:
            logger.error(f"Error setting experience level filter: {e}")

    async def _set_job_type_filter(self):
        """Set job type filter (async)"""
        if not self.job_types:
            return

        try:
            job_type_mapping = {
                "full_time": "Full-time",
                "contract": "Contract",
                "part_time": "Part-time",
                "temporary": "Temporary",
                "volunteer": "Volunteer",
                "internship": "Internship",
                "other": "Other",
            }

            for job_type_key, is_enabled in self.job_types.items():
                if is_enabled and job_type_key in job_type_mapping:
                    job_type_text = job_type_mapping[job_type_key]
                    # There are two internship checkboxes, so we need to select the second one
                    element_number = 1 if job_type_key == "internship" else 0

                    # Try multiple selector approaches
                    job_type_selectors = [
                        f"//label[contains(., '{job_type_text}')]",
                        f"//input[@value='{job_type_text}']/..",
                        f"label:has-text('{job_type_text}')",
                        f"[data-test-job-type-filter='{job_type_key}']",
                    ]

                    job_type_set = False
                    for selector in job_type_selectors:
                        if await safe_click(self.page, selector, element_number=element_number):
                            logger.info(f"Job type set: {job_type_text}")
                            job_type_set = True
                            break

                    if not job_type_set:
                        logger.warning(f"Element not found for job type: {job_type_text}")

                    # await async_pause()

        except Exception as e:
            logger.error(f"Error setting job type filter: {e}")

    async def _set_work_location_filter(self):
        """Set work location filter (remote/hybrid/on-site) - async"""
        try:
            work_location_filters = []
            if self.remote:
                work_location_filters.append("Remote")
            if self.hybrid:
                work_location_filters.append("Hybrid")
            if self.onsite:
                work_location_filters.append("On-site")

            for location_type in work_location_filters:
                # Try multiple selector approaches
                location_selectors = [
                    f"//label[contains(., '{location_type}')]",
                    f"//input[@value='{location_type}']/..",
                    f"label:has-text('{location_type}')",
                    f"[data-test-work-location-filter='{location_type.lower()}']",
                ]

                location_set = False
                for selector in location_selectors:
                    if await safe_click(self.page, selector):
                        logger.info(f"Location type set: {location_type}")
                        location_set = True
                        break

                if not location_set:
                    logger.warning(f"Element not found for location type: {location_type}")

                # await async_pause()

        except Exception as e:
            logger.error(f"Error setting work location filter: {e}")

    async def _apply_filters(self):
        """Apply set filters (async)"""
        try:
            # Find and click the "Show results" or "Apply" button
            apply_selectors = [
                "//button[contains(., 'Show') or contains(., 'Apply') or contains(., 'Done')]",
                "button[aria-label*='Show results']",
                "button[aria-label*='Apply filters']",
                ".jobs-search-dropdown__apply-button",
                ".search-reusables__filter-pill-button",
            ]

            for selector in apply_selectors:
                if await safe_click(self.page, selector, timeout=10000):
                    # await async_pause()
                    logger.info("Filters applied")
                    return True

            logger.warning("Could not find or click filter apply button")
            return False

        except Exception as e:
            logger.error(f"Error applying filters: {e}")
        return False

    async def _audit_easy_apply_filter_state(self) -> None:
        """Audit and repair persisted LinkedIn Easy Apply state."""
        try:
            current_url = str(getattr(self.page, "url", "") or "")

            def url_has_easy_apply(url: str) -> bool:
                try:
                    query = dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))
                    value = str(query.get("f_AL", "")).strip().lower()
                    return value in {"true", "1", "yes"}
                except Exception:
                    return "f_AL=true" in url or "f_AL=1" in url

            async def read_ui_state():
                selectors = [
                    "//h3[contains(., 'Easy Apply')]/following::input[@role='switch'][1]",
                    "input[role='switch'][aria-label*='Easy Apply' i]",
                    "input[role='switch'][id*='easy-apply' i]",
                ]
                for selector in selectors:
                    element = await find_element_safely(self.page, selector)
                    if not element:
                        continue
                    value = await element.get_attribute("aria-checked")
                    if value in ("true", "false"):
                        return value == "true"
                return None

            ui_enabled = await read_ui_state()
            url_enabled = url_has_easy_apply(current_url)

            logger.info(
                "POST-FILTER EASY APPLY AUDIT | "
                f"config={EASY_APPLY_ONLY_MODE} | "
                f"ui={ui_enabled} | "
                f"url_f_AL={url_enabled} | "
                f"action={'none' if ui_enabled is not True and not url_enabled else 'repair'} | "
                f"pass={ui_enabled is not True and not url_enabled}"
            )

            if EASY_APPLY_ONLY_MODE:
                return

            if ui_enabled is not True and not url_enabled:
                logger.info(
                    "POST-FILTER EASY APPLY AUDIT OK | external mode has Easy Apply OFF"
                )
                return

            logger.warning(
                "POST-FILTER EASY APPLY REPAIR | persisted Easy Apply state detected"
            )

            if ui_enabled is True:
                toggle_selectors = [
                    "//h3[contains(., 'Easy Apply')]/following::div[contains(@class, 'artdeco-toggle')][1]",
                    "label[data-artdeco-toggle-label='true']:has(span:text('Toggle Easy Apply filter'))",
                    "//h3[contains(., 'Easy Apply')]/following::span[@data-artdeco-toggle-text='true'][1]",
                ]
                for selector in toggle_selectors:
                    if await safe_click(self.page, selector):
                        await async_pause(1, 1)
                        break

            current_url = str(getattr(self.page, "url", "") or current_url)

            if url_has_easy_apply(current_url):
                parts = urlsplit(current_url)
                query_pairs = [
                    (k, v)
                    for k, v in parse_qsl(parts.query, keep_blank_values=True)
                    if k != "f_AL"
                ]
                clean_url = urlunsplit(
                    (
                        parts.scheme,
                        parts.netloc,
                        parts.path,
                        urlencode(query_pairs, doseq=True),
                        parts.fragment,
                    )
                )

                if clean_url != current_url:
                    logger.info(
                        "POST-FILTER EASY APPLY REPAIR | removing persisted f_AL from URL"
                    )
                    await self.page.goto(clean_url, wait_until="domcontentloaded")
                    await async_pause(2, 3)

            final_url = str(getattr(self.page, "url", "") or "")
            final_ui = await read_ui_state()
            final_url_enabled = url_has_easy_apply(final_url)

            logger.info(
                "POST-FILTER EASY APPLY AUDIT FINAL | "
                f"config={EASY_APPLY_ONLY_MODE} | "
                f"ui={final_ui} | "
                f"url_f_AL={final_url_enabled} | "
                f"action=repair | pass={final_ui is not True and not final_url_enabled}"
            )

            if final_ui is True or final_url_enabled:
                logger.warning(
                    "POST-FILTER EASY APPLY AUDIT FAILED | "
                    "Easy Apply still appears enabled after repair"
                )
            else:
                logger.info(
                    "POST-FILTER EASY APPLY AUDIT PASSED | "
                    "Easy Apply is OFF after filter application"
                )

        except Exception as e:
            logger.warning(
                f"POST-FILTER EASY APPLY AUDIT ERROR | {e}"
            )

    async def set_search_params(self):
        """Set search parameters on LinkedIn (async)"""
        logger.info("Starting LinkedIn search parameters setup")
        self.filters_verified = False

        try:
            if runtime_controller.is_shutdown_requested():
                logger.info("Shutdown requested — search setup is closed")
                return False
            if LINKEDIN_RECOMMENDED_JOBS_MODE:
                await self._open_recommended_jobs()
                logger.error(
                    "FILTER_GATE_FAILED | recommended collection does not expose the "
                    "configured profile filter state"
                )
                return False
            if LINKEDIN_TOP_APPLICANT_JOBS_MODE:
                await self._open_top_applicant_jobs()
                logger.error(
                    "FILTER_GATE_FAILED | top-applicant collection does not expose the "
                    "configured profile filter state"
                )
                return False

            # Navigate to LinkedIn jobs search. Keep the established UI route
            # for compatibility with LinkedIn's normal filter controls, then
            # replace the resulting URL with a clean profile URL. The gate is
            # still closed until the clean URL and the visible filter dialog
            # both verify successfully.
            await self.page.goto(
                "https://www.linkedin.com/jobs/search/", wait_until="domcontentloaded"
            )
            await async_pause(2, 3)
            if runtime_controller.is_shutdown_requested():
                logger.info("Shutdown requested — stopping before search terms")
                return False
            await self._set_basic_search_terms()
            await self._commit_basic_search()
            if runtime_controller.is_shutdown_requested():
                return False
            filters_open = await self._open_all_filters()
            if filters_open:
                await self._set_date_posted_filter()
                await self._set_experience_level_filter()
                await self._set_job_type_filter()
                await self._set_work_location_filter()
                await self._set_easy_apply_filter()
                if not await self._apply_filters():
                    logger.error("FILTER_APPLY_FAILED | filter dialog did not apply")
                    return False

            # Test doubles and a browser that failed to expose a usable URL
            # cannot establish a filter gate. In production, navigate to a
            # deterministic URL that removes every stale managed parameter.
            current_url = str(getattr(self.page, "url", "") or "")
            if not current_url.startswith(("http://", "https://")):
                logger.error("FILTER_GATE_FAILED | browser URL unavailable")
                return False
            if not await self._establish_and_verify_profile_filters():
                return False

            logger.info("Search parameters successfully set and verified")
            return True

        except Exception as e:
            logger.error(f"Error setting search parameters: {e}")
            raise

    async def _set_easy_apply_filter(self):
        """Set Easy Apply filter toggle (async)"""
        if not EASY_APPLY_ONLY_MODE:
            # Force-disable persisted LinkedIn Easy Apply filter.
            # LinkedIn can keep this toggle enabled from a previous session.
            try:
                input_selectors = [
                    "//h3[contains(., 'Easy Apply')]/following::input[@role='switch'][1]",
                    "input[role='switch'][aria-label*='Easy Apply' i]",
                    "input[role='switch'][id*='easy-apply' i]",
                ]

                current_enabled = None
                for input_selector in input_selectors:
                    input_element = await find_element_safely(
                        self.page, input_selector
                    )
                    if input_element:
                        aria_checked = await input_element.get_attribute(
                            "aria-checked"
                        )
                        if aria_checked in ("true", "false"):
                            current_enabled = aria_checked == "true"
                            break

                if current_enabled is False:
                    logger.info(
                        "Easy Apply filter already disabled for external mode"
                    )
                    return

                if current_enabled is None:
                    logger.warning(
                        "Could not determine Easy Apply filter state in external mode"
                    )
                    return

                easy_apply_selectors = [
                    "//h3[contains(., 'Easy Apply')]/following::div[contains(@class, 'artdeco-toggle')][1]",
                    "label[data-artdeco-toggle-label='true']:has(span:text('Toggle Easy Apply filter'))",
                    "//h3[contains(., 'Easy Apply')]/following::span[@data-artdeco-toggle-text='true'][1]",
                ]

                for selector in easy_apply_selectors:
                    if await safe_click(self.page, selector):
                        await async_pause(1, 1)
                        logger.info(
                            "Easy Apply filter disabled for external mode"
                        )
                        return

                logger.warning(
                    "Easy Apply filter is enabled but bot could not disable it"
                )
            except Exception as e:
                logger.error(
                    f"Error disabling Easy Apply filter for external mode: {e}"
                )
            return

        try:
            # First check if Easy Apply is already enabled
            input_selectors = [
                "//h3[contains(., 'Easy Apply')]/following::input[@role='switch'][1]",
                "input[role='switch'][aria-label*='Easy Apply' i]",
                "input[role='switch'][id*='easy-apply' i]",
            ]

            for input_selector in input_selectors:
                input_element = await find_element_safely(self.page, input_selector)
                if input_element:
                    aria_checked = await input_element.get_attribute("aria-checked")
                    if aria_checked == "true":
                        logger.info("Easy Apply filter is already enabled")
                        return
                    break

            # Easy Apply is a toggle switch - click on the label or parent div, not the input
            easy_apply_selectors = [
                # Click on the parent div toggle container
                "//h3[contains(., 'Easy Apply')]/following::div[contains(@class, 'artdeco-toggle')][1]",
                # Alternative: find label by text
                "label[data-artdeco-toggle-label='true']:has(span:text('Toggle Easy Apply filter'))",
                # Fallback: click on the toggle text span
                "//h3[contains(., 'Easy Apply')]/following::span[@data-artdeco-toggle-text='true'][1]",
            ]

            easy_apply_toggled = False
            for selector in easy_apply_selectors:
                if await safe_click(self.page, selector):
                    logger.info("Easy Apply filter enabled")
                    easy_apply_toggled = True
                    # await async_pause()
                    break

            if not easy_apply_toggled:
                logger.warning("Could not find or toggle Easy Apply filter")

        except Exception as e:
            logger.error(f"Error setting Easy Apply filter: {e}")


if __name__ == "__main__":
    """Simple test for SearchCustomizer functionality"""
    import asyncio

    from src.utils.browser_utils import create_playwright_browser, save_browser_session

    # Test configuration
    test_config = {
        "remote": True,
        "hybrid": True,
        "onsite": False,
        "experience_level": {
            "entry": True,
            "associate": True,
            "mid_senior_level": True,
            "director": False,
            "executive": False,
            "internship": False,
        },
        "job_types": {
            "full_time": True,
            "contract": False,
            "part_time": True,
            "temporary": True,
            "volunteer": False,
            "internship": False,
        },
        "date": {"all_time": False, "month": False, "week": False, "24_hours": True},
        "positions": ["Software Engineer", "Python Developer"],
        "locations": ["Germany"],
        "apply_once_at_company": True,
        "company_blacklist": ["wayfair", "Crossover"],
        "title_blacklist": ["word1", "word2"],
        "location_blacklist": ["Brazil"],
    }

    def parse_args():
        parser = argparse.ArgumentParser(description="Debug LinkedIn job search filters safely")
        parser.add_argument(
            "--config",
            action="store_true",
            help="Load config/search_config.yaml instead of the built-in smoke-test config",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=25,
            help="Maximum visible result cards to print",
        )
        parser.add_argument(
            "--pause-seconds",
            type=int,
            default=300,
            help="Seconds to keep the browser open for manual inspection after parsing",
        )
        return parser.parse_args()

    def _canonical_job_url_from_href(href: str | None) -> str:
        if not href:
            return ""

        current_job_match = re.search(r"[?&]currentJobId=(\d+)", href)
        if current_job_match:
            return f"https://www.linkedin.com/jobs/view/{current_job_match.group(1)}"

        view_match = re.search(r"/jobs/view/(\d+)", href)
        if view_match:
            return f"https://www.linkedin.com/jobs/view/{view_match.group(1)}"

        return href

    async def _first_text(element: Any, selectors: list[str]) -> str:
        for selector in selectors:
            try:
                locator = element.locator(selector).first
                if await locator.count() > 0:
                    text = (
                        await locator.inner_text() or await locator.text_content() or ""
                    ).strip()
                    if text:
                        return " ".join(text.split())
            except Exception:
                continue
        return ""

    async def _first_href(element: Any, selectors: list[str]) -> str:
        for selector in selectors:
            try:
                locator = element.locator(selector).first
                if await locator.count() > 0:
                    href = await locator.get_attribute("href")
                    if href:
                        return _canonical_job_url_from_href(href)
            except Exception:
                continue
        return ""

    def _fallback_job_card_fields(text: str) -> tuple[str, str, str]:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        filtered = [
            line
            for line in lines
            if line.lower() not in {"promoted", "easy apply", "view job", "actively hiring"}
        ]
        title = filtered[0] if len(filtered) > 0 else ""
        company = filtered[1] if len(filtered) > 1 else ""
        location = filtered[2] if len(filtered) > 2 else ""
        return title, company, location

    def is_applied_job_card_text(text: str) -> bool:
        return any(line.strip().lower() == "applied" for line in text.splitlines())

    async def is_applied_search_result_card(card: Any, full_text: str = "") -> bool:
        """Return True when a visible search result card is marked Applied."""
        selectors = [
            ".job-card-container__footer-job-state",
            ".job-card-container__footer-wrapper",
            "li",
        ]
        for selector in selectors:
            try:
                locator = card.locator(selector)
                if isawaitable(locator):
                    continue
                count = await locator.count()
                for index in range(count):
                    item = locator.nth(index)
                    text = (
                        (await item.inner_text() or await item.text_content() or "").strip().lower()
                    )
                    if text == "applied":
                        return True
            except Exception:
                continue

        return is_applied_job_card_text(full_text)

    async def parse_visible_search_results(page: Any, limit: int = 25) -> list[dict[str, str]]:
        """Parse visible LinkedIn search result cards without opening/applying to jobs."""
        card_selectors = [
            ".scaffold-layout__list [data-view-name='job-card'][data-job-id]",
            ".scaffold-layout__list .job-card-job-posting-card-wrapper[data-job-id]",
            ".scaffold-layout__list div[data-job-id]",
            ".jobs-search-results__list-item",
            ".job-card-container",
            "div[data-job-id]",
        ]
        title_selectors = [
            "a[href*='/jobs/view/']",
            "a[href*='currentJobId=']",
            ".job-card-list__title",
            ".job-card-container__link",
        ]
        company_selectors = [
            ".artdeco-entity-lockup__subtitle",
            ".job-card-container__primary-description",
            "a[href*='/company/']",
        ]
        location_selectors = [
            ".artdeco-entity-lockup__caption",
            ".job-card-container__metadata-item",
            "li-icon[type='map-marker-icon'] ~ span",
        ]
        link_selectors = ["a[href*='/jobs/view/']", "a[href*='currentJobId=']"]

        seen_urls = set()
        results = []
        for selector in card_selectors:
            by = "xpath" if selector.startswith("//") else "css selector"
            cards = await find_elements_safely(page, selector, by)
            if not cards:
                continue

            for card in cards:
                if len(results) >= limit:
                    break
                try:
                    full_text = await get_clean_text(card)
                    is_applied = await is_applied_search_result_card(card, full_text)
                    fallback_title, fallback_company, fallback_location = _fallback_job_card_fields(
                        full_text
                    )
                    url = await _first_href(card, link_selectors)
                    if not url:
                        job_id = await card.get_attribute(
                            "data-job-id"
                        ) or await card.get_attribute("data-occludable-job-id")
                        if job_id:
                            url = f"https://www.linkedin.com/jobs/view/{job_id}"
                    if url and url in seen_urls:
                        continue
                    if url:
                        seen_urls.add(url)

                    results.append(
                        {
                            "title": await _first_text(card, title_selectors) or fallback_title,
                            "company": await _first_text(card, company_selectors)
                            or fallback_company,
                            "location": await _first_text(card, location_selectors)
                            or fallback_location,
                            "url": url,
                            "skip_reason": "Already applied" if is_applied else "",
                        }
                    )
                except Exception as e:
                    logger.debug(f"Failed parsing visible job card: {e}")

            if results:
                break

        return results

    def load_debug_search_config(use_real_config: bool) -> dict[str, Any]:
        if use_real_config:
            logger.info(f"Loading real search config: {SEARCH_CONFIG_FILE}")
            config = load_yaml_file(SEARCH_CONFIG_FILE)
            if not isinstance(config, dict):
                raise ValueError(f"Search config {SEARCH_CONFIG_FILE} must be a mapping")
            return config
        logger.info("Using built-in smoke-test search config")
        return test_config

    def log_search_results(results: list[dict[str, str]]) -> None:
        logger.info(f"Visible LinkedIn search results parsed: {len(results)}")
        if not results:
            logger.warning("No visible job result cards were parsed")
            return
        for index, job in enumerate(results, start=1):
            skip_prefix = f"[SKIP: {job['skip_reason']}] " if job.get("skip_reason") else ""
            logger.info(
                f"[{index}] {skip_prefix}{job.get('title') or '-'} | "
                f"{job.get('company') or '-'} | "
                f"{job.get('location') or '-'} | "
                f"{job.get('url') or '-'}"
            )

    async def test_search_customizer():
        """Async test function for SearchCustomizer"""
        args = parse_args()
        browser = None
        context = None

        try:
            # Initialize Playwright browser (async)
            browser, context, page = await create_playwright_browser()
            page = page
            logger.info("Playwright browser initialized successfully (async)")

            # Create SearchCustomizer instance
            search_customizer = SearchCustomizer(page)

            # Test parameter setting
            search_customizer.set_advanced_search_params(load_debug_search_config(args.config))
            logger.info("✓ Parameters set successfully")

            # Test blacklist functionality
            if not args.config:
                test_cases = [
                    ("Software Engineer", "Wayfair", "Germany", True),  # Company blacklisted
                    ("Python Developer", "Google", "Brazil", True),  # Location blacklisted
                    ("word1 Developer", "Microsoft", "Germany", True),  # Title blacklisted
                    ("Data Scientist", "Amazon", "Germany", False),  # Not blacklisted
                ]

                for title, company, location, expected in test_cases:
                    result = search_customizer.is_job_blacklisted(title, company, location)
                    status = "✓" if result == expected else "✗"
                    logger.info(
                        f"{status} Blacklist test: {title} at {company} in {location} -> {result}"
                    )

            logger.info("✓ All tests completed successfully")

            # Test async set_search_params
            await search_customizer.set_search_params()
            await async_pause(3, 5)
            results = await parse_visible_search_results(page, limit=args.limit)
            log_search_results(results)

            if args.pause_seconds > 0:
                logger.info(
                    f"Search debug complete. Browser will remain open for {args.pause_seconds} seconds."
                )
                await async_pause(args.pause_seconds, args.pause_seconds)

        except Exception as e:
            logger.error(f"Test failed: {e}")
        finally:
            # Cleanup Playwright resources
            logger.info("Cleaning up Playwright browser resources...")
            try:
                if context:
                    # Save session state before closing (async)
                    await save_browser_session(context)

                if browser:
                    await browser.close()
                    logger.info("Playwright browser closed successfully")
            except Exception as cleanup_error:
                logger.warning(f"Error during Playwright cleanup: {cleanup_error}")

    # Run the async test
    asyncio.run(test_search_customizer())
