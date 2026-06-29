import os
import time
import threading
from typing import Any, Dict, List, Optional, Tuple

import requests

import database
from jplan_logging import jlog


ORS_BASE_URL = "https://api.openrouteservice.org"
NOMINATIM_SEARCH_URL = "https://nominatim.openstreetmap.org/search"
MALAYSIA_COUNTRY_CODE = "MYS"
MALAYSIA_NOMINATIM_COUNTRY_CODE = "my"
CYBERJAYA_FOCUS_LAT = 2.9264
CYBERJAYA_FOCUS_LON = 101.6412
DEFAULT_NOMINATIM_MIN_INTERVAL_SECONDS = 1.0

_NOMINATIM_LOCK = threading.Lock()
_LAST_NOMINATIM_REQUEST_AT = 0.0


class TravelServiceError(Exception):
    pass


class MissingORSApiKey(TravelServiceError):
    pass


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _clean_key(value: Any) -> str:
    return " ".join(_clean(value).lower().split())


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return _clean_key(raw) not in {"0", "false", "no", "off", "disabled"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _cache_country_hint(query: str) -> Optional[str]:
    return MALAYSIA_NOMINATIM_COUNTRY_CODE if _looks_malaysia_scoped(query) else None


def _cache_category_hint(category: Optional[str]) -> Optional[str]:
    clean = _clean_key(category)
    return clean or None


def _rate_limited_error(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    return status_code == 429 or "429" in str(exc) or "rate" in _clean_key(str(exc))


def _coord_from_location(location: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    lat = location.get("latitude") if location.get("latitude") is not None else location.get("lat")
    lng = location.get("longitude") if location.get("longitude") is not None else location.get("lng")
    try:
        if lat is None or lng is None:
            return None
        return float(lat), float(lng)
    except (TypeError, ValueError):
        return None


def _looks_malaysia_scoped(query: str) -> bool:
    clean = _clean_key(query)
    malaysia_terms = {
        "malaysia",
        "cyberjaya",
        "selangor",
        "putrajaya",
        "kuala lumpur",
        "kl",
        "mmu",
        "multimedia university",
    }
    return any(term in clean for term in malaysia_terms)


def _candidate_text(candidate: Dict[str, Any]) -> str:
    return _clean_key(" ".join(str(candidate.get(field) or "") for field in ("display_name", "address", "country", "region")))


def _significant_query_tokens(query: str) -> List[str]:
    stop_words = {
        "the",
        "a",
        "an",
        "at",
        "in",
        "near",
        "malaysia",
        "cyberjaya",
        "selangor",
        "putrajaya",
        "kuala",
        "lumpur",
        "kl",
    }
    tokens = []
    for token in _clean_key(query).replace("@", " ").split():
        cleaned = "".join(ch for ch in token if ch.isalnum())
        if len(cleaned) >= 3 and cleaned not in stop_words:
            tokens.append(cleaned)
    return tokens


def _candidate_looks_related(query: str, candidate: Dict[str, Any]) -> bool:
    text = _candidate_text(candidate)
    tokens = _significant_query_tokens(query)
    if not tokens:
        return True
    return any(token in text.split() for token in tokens)


def _candidate_name_key(candidate: Dict[str, Any]) -> str:
    text = _candidate_text(candidate)
    for noise in ("@", "&", "-", "_", ","):
        text = text.replace(noise, " ")
    tokens = [
        token
        for token in text.split()
        if token not in {
            "the",
            "cyberjaya",
            "cyber",
            "selangor",
            "sepang",
            "malaysia",
            "63000",
            "persiaran",
            "bestari",
            "jalan",
        }
    ]
    return " ".join(tokens[:4])


def _candidate_is_duplicate(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
    try:
        lat_delta = abs(float(left.get("latitude")) - float(right.get("latitude")))
        lng_delta = abs(float(left.get("longitude")) - float(right.get("longitude")))
    except (TypeError, ValueError):
        return False
    if lat_delta > 0.0015 or lng_delta > 0.0015:
        return False

    left_name = _candidate_name_key(left)
    right_name = _candidate_name_key(right)
    if not left_name or not right_name:
        return False
    left_tokens = set(left_name.split())
    right_tokens = set(right_name.split())
    overlap = left_tokens & right_tokens
    if bool(overlap) and len(overlap) >= min(len(left_tokens), len(right_tokens), 2):
        return True
    if left_name in right_name or right_name in left_name:
        return True
    compact_left = left_name.replace(" ", "")
    compact_right = right_name.replace(" ", "")
    return bool(compact_left and compact_right) and (compact_left in compact_right or compact_right in compact_left)


def _candidate_matches_region(query: str, candidate: Dict[str, Any]) -> bool:
    if not _looks_malaysia_scoped(query):
        return True
    text = _candidate_text(candidate)
    return any(term in text for term in ("malaysia", "cyberjaya", "selangor", "putrajaya", "kuala lumpur"))


class TravelService:
    """Small ORS-backed travel/geocoding layer used only for accurate travel mode."""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("OPENROUTESERVICE_API_KEY") or os.getenv("ORS_API_KEY")
        self.route_cache: Dict[Tuple[float, float, float, float, str, Optional[str]], int] = {}
        self.route_stats: Dict[str, Any] = {
            "route_api_calls": 0,
            "route_cache_hits": 0,
            "route_cache_misses": 0,
            "route_persistent_cache_hits": 0,
            "route_fetch_seconds": 0.0,
            "geocode_seconds": 0.0,
        }
        self.geocode_memory_cache: Dict[Tuple[str, str, Optional[str], Optional[str]], List[Dict[str, Any]]] = {}
        self.nominatim_cache: Dict[Tuple[str, Optional[str], Optional[str]], List[Dict[str, Any]]] = {}
        self.enable_nominatim_fallback = _env_bool("ENABLE_NOMINATIM_FALLBACK", True)
        self.nominatim_min_interval_seconds = max(
            0.0,
            _env_float("NOMINATIM_MIN_INTERVAL_SECONDS", DEFAULT_NOMINATIM_MIN_INTERVAL_SECONDS),
        )
        self.geocode_cache_ttl_days = _env_int("GEOCODE_CACHE_TTL_DAYS", 30)
        self.route_cache_ttl_days = _env_int("ROUTE_CACHE_TTL_DAYS", 30)
        self.nominatim_user_agent = (
            os.getenv("JPLAN_NOMINATIM_USER_AGENT")
            or "JPlan-FYP/1.0 (location search; contact: local-development)"
        )

    def has_api_key(self) -> bool:
        return bool(_clean(self.api_key))

    def reset_stats(self) -> None:
        self.route_stats = {
            "route_api_calls": 0,
            "route_cache_hits": 0,
            "route_cache_misses": 0,
            "route_persistent_cache_hits": 0,
            "route_fetch_seconds": 0.0,
            "geocode_seconds": 0.0,
        }

    def stats_snapshot(self) -> Dict[str, Any]:
        return dict(self.route_stats)

    def expand_alias(self, label: str, category: Optional[str] = None) -> str:
        clean = _clean_key(label)
        if clean in {"mmu", "multimedia university"}:
            return "Multimedia University Cyberjaya, Selangor, Malaysia"
        if clean in {"campus", "school", "university"}:
            return "Multimedia University Cyberjaya, Selangor, Malaysia"
        if clean in {"main office", "office"}:
            return "Main Office Cyberjaya, Selangor, Malaysia"
        if clean in {"library"}:
            return "Library Cyberjaya, Selangor, Malaysia"
        if clean in {"gym", "fitness center"}:
            return "Gym Cyberjaya, Selangor, Malaysia"
        if category == "supermarket" and clean in {"store", "supermarket", "market"}:
            return "Supermarket Cyberjaya, Selangor, Malaysia"
        return label

    def saved_location_matches(
        self,
        label: Optional[str],
        category: Optional[str],
        saved_locations: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        label_key = _clean_key(label)
        category_key = _clean_key(category)
        alias_groups = {
            "campus_area": {"campus", "school", "mmu", "university"},
            "school": {"campus", "school", "mmu", "university"},
            "office": {"office", "main office", "work"},
            "library": {"library"},
            "fitness_center": {"gym", "fitness center", "fitness"},
            "supermarket": {"store", "supermarket", "market", "grocery", "groceries"},
            "home": {"home", "house"},
        }
        wanted = {label_key}
        wanted.update(alias_groups.get(category_key, set()))
        wanted.update(alias_groups.get(label_key, set()))
        wanted = {item for item in wanted if item}

        matches: List[Dict[str, Any]] = []
        for saved in saved_locations or []:
            haystack = _clean_key(
                " ".join(
                    str(saved.get(field) or "")
                    for field in ("label", "display_name", "address", "category", "type")
                )
            )
            if not haystack:
                continue
            if any(token and token in haystack for token in wanted):
                matches.append(saved)
        return matches

    def confirmed_saved_location(
        self,
        label: Optional[str],
        category: Optional[str],
        saved_locations: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        for saved in self.saved_location_matches(label, category, saved_locations):
            if _coord_from_location(saved):
                return saved
        return None

    def format_saved_match(self, saved: Dict[str, Any]) -> Dict[str, Any]:
        coord = _coord_from_location(saved)
        return {
            "label": saved.get("label"),
            "display_name": saved.get("display_name") or saved.get("label"),
            "address": saved.get("address"),
            "latitude": coord[0] if coord else None,
            "longitude": coord[1] if coord else None,
            "source": saved.get("source") or "saved_profile",
            "confirmed_by_user": bool(saved.get("confirmed_by_user", True)),
        }

    def geocode_candidates(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        return self.geocode_candidates_with_metadata(query, limit=limit).get("candidates", [])

    def geocode_candidates_with_metadata(
        self,
        query: str,
        category: Optional[str] = None,
        limit: int = 5,
    ) -> Dict[str, Any]:
        started = time.perf_counter()
        limit = max(1, min(limit, 5))
        country_hint = _cache_country_hint(query)
        category_hint = _cache_category_hint(category)
        providers_used: List[str] = []
        warnings: List[str] = []
        ors_candidates: List[Dict[str, Any]] = []
        ors_error: Optional[Exception] = None

        if not self.has_api_key():
            ors_candidates = []
            warnings.append("OpenRouteService API key is missing; geocoding cannot use the primary provider.")
        else:
            try:
                ors_candidates = self._ors_geocode_candidates(query, limit, country_hint, category_hint)
                providers_used.append("ors")
            except Exception as exc:
                ors_error = exc
                warnings.append(f"OpenRouteService geocoding failed: {exc}")

        nominatim_candidates: List[Dict[str, Any]] = []
        if not self._should_try_nominatim(query, ors_candidates):
            jlog("TRAVEL_SERVICE", "fallback_disabled_or_skipped reason=ors_good", "NOMINATIM")
        elif not self.enable_nominatim_fallback:
            jlog("TRAVEL_SERVICE", "fallback_disabled_or_skipped reason=disabled", "NOMINATIM")
            warnings.append("OpenStreetMap fallback search is disabled; use the map picker if the results are not right.")
        elif not self.has_api_key() and ors_error is None:
            jlog("TRAVEL_SERVICE", "fallback_disabled_or_skipped reason=ors_api_key_missing", "NOMINATIM")
            warnings.append("Use the map picker because ORS geocoding is not configured.")
        else:
            try:
                nominatim_candidates = self._nominatim_geocode_candidates(query, limit, country_hint, category_hint)
                providers_used.append("nominatim")
            except Exception as exc:
                if _rate_limited_error(exc):
                    warnings.append("OpenStreetMap fallback search is rate-limited; you can still pick the location manually on the map.")
                else:
                    warnings.append("OpenStreetMap fallback search is currently unavailable; you can still pick the location manually on the map.")
                nominatim_candidates = []

        merged = self._merge_geocode_candidates(ors_candidates, nominatim_candidates, limit)
        if merged and warnings:
            status = "partial"
        elif merged:
            status = "ok"
        elif any("rate" in _clean_key(item) for item in warnings):
            status = "rate_limited"
        else:
            status = "fallback_unavailable"

        try:
            return {
                "candidates": merged,
                "geocode_status": status,
                "providers_used": providers_used,
                "warnings": warnings,
                "warning": warnings[0] if warnings else None,
                "primary_error": str(ors_error) if ors_error else None,
            }
        finally:
            self.route_stats["geocode_seconds"] = float(self.route_stats.get("geocode_seconds") or 0.0) + (
                time.perf_counter() - started
            )

    def _cache_get(
        self,
        provider: str,
        normalized_query: str,
        country_hint: Optional[str],
        category_hint: Optional[str],
        limit: int,
    ) -> Optional[List[Dict[str, Any]]]:
        memory_key = (provider, normalized_query, country_hint, category_hint)
        if memory_key in self.geocode_memory_cache:
            if provider == "nominatim":
                jlog("TRAVEL_SERVICE", f"cache_hit query={normalized_query}", "NOMINATIM")
            return self.geocode_memory_cache[memory_key][:limit]

        cached = database.get_geocode_cache(
            normalized_query=normalized_query,
            provider=provider,
            country_hint=country_hint,
            category_hint=category_hint,
        )
        if cached is not None:
            candidates = cached if isinstance(cached, list) else []
            self.geocode_memory_cache[memory_key] = candidates
            if provider == "nominatim":
                jlog("TRAVEL_SERVICE", f"cache_hit query={normalized_query}", "NOMINATIM")
            return candidates[:limit]
        return None

    def _cache_set(
        self,
        provider: str,
        normalized_query: str,
        country_hint: Optional[str],
        category_hint: Optional[str],
        candidates: List[Dict[str, Any]],
    ) -> None:
        memory_key = (provider, normalized_query, country_hint, category_hint)
        self.geocode_memory_cache[memory_key] = candidates
        database.save_geocode_cache(
            normalized_query=normalized_query,
            provider=provider,
            result_json=candidates,
            country_hint=country_hint,
            category_hint=category_hint,
            ttl_days=self.geocode_cache_ttl_days,
        )

    def _ors_geocode_candidates(
        self,
        query: str,
        limit: int,
        country_hint: Optional[str] = None,
        category_hint: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        normalized_query = _clean_key(query)
        cached = self._cache_get("ors", normalized_query, country_hint, category_hint, limit)
        if cached is not None:
            return cached

        params = {
            "api_key": self.api_key,
            "text": query,
            "size": limit,
        }
        if _looks_malaysia_scoped(query):
            # Pelias/OpenRouteService geocoding is global. These hints keep
            # Malaysia-specific searches from being outranked by UK/US matches.
            params["boundary.country"] = MALAYSIA_COUNTRY_CODE
            params["focus.point.lat"] = CYBERJAYA_FOCUS_LAT
            params["focus.point.lon"] = CYBERJAYA_FOCUS_LON
        response = requests.get(f"{ORS_BASE_URL}/geocode/search", params=params, timeout=12)
        response.raise_for_status()
        payload = response.json()
        candidates: List[Dict[str, Any]] = []
        for feature in payload.get("features") or []:
            geometry = feature.get("geometry") or {}
            coords = geometry.get("coordinates") or []
            if len(coords) < 2:
                continue
            props = feature.get("properties") or {}
            candidates.append({
                "display_name": props.get("label") or props.get("name") or props.get("street"),
                "address": props.get("label") or props.get("name"),
                "latitude": float(coords[1]),
                "longitude": float(coords[0]),
                "confidence": props.get("confidence"),
                "source": "ors_geocoded",
                "country": props.get("country"),
                "region": props.get("region") or props.get("county"),
            })
        candidates = candidates[:limit]
        self._cache_set("ors", normalized_query, country_hint, category_hint, candidates)
        return candidates

    def _should_try_nominatim(self, query: str, ors_candidates: List[Dict[str, Any]]) -> bool:
        if not ors_candidates:
            return True
        if _looks_malaysia_scoped(query) and not any(_candidate_matches_region(query, item) for item in ors_candidates):
            return True
        if not any(_candidate_looks_related(query, item) for item in ors_candidates):
            return True
        return False

    def _nominatim_geocode_candidates(
        self,
        query: str,
        limit: int,
        country_hint: Optional[str] = None,
        category_hint: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        normalized_query = _clean_key(query)
        cache_key = (normalized_query, country_hint, category_hint)
        cached = self._cache_get("nominatim", normalized_query, country_hint, category_hint, limit)
        if cached is not None:
            self.nominatim_cache[cache_key] = cached
            return cached

        global _LAST_NOMINATIM_REQUEST_AT

        params = {
            "q": query,
            "format": "jsonv2",
            "limit": limit,
            "addressdetails": 1,
            "namedetails": 1,
        }
        if _looks_malaysia_scoped(query):
            params["countrycodes"] = MALAYSIA_NOMINATIM_COUNTRY_CODE

        with _NOMINATIM_LOCK:
            cached = self._cache_get("nominatim", normalized_query, country_hint, category_hint, limit)
            if cached is not None:
                self.nominatim_cache[cache_key] = cached
                return cached

            elapsed = time.monotonic() - _LAST_NOMINATIM_REQUEST_AT
            if elapsed < self.nominatim_min_interval_seconds:
                wait_seconds = self.nominatim_min_interval_seconds - elapsed
                jlog("TRAVEL_SERVICE", f"throttled wait={wait_seconds:.2f}s query={normalized_query}", "NOMINATIM")
                time.sleep(wait_seconds)

            jlog("TRAVEL_SERVICE", f"request_sent query={normalized_query}", "NOMINATIM")
            _LAST_NOMINATIM_REQUEST_AT = time.monotonic()
            response = requests.get(
                NOMINATIM_SEARCH_URL,
                params=params,
                headers={"User-Agent": self.nominatim_user_agent},
                timeout=12,
            )
            response.raise_for_status()

        payload = response.json()
        if not isinstance(payload, list):
            self.nominatim_cache[cache_key] = []
            self._cache_set("nominatim", normalized_query, country_hint, category_hint, [])
            return []

        candidates: List[Dict[str, Any]] = []
        for item in payload:
            try:
                lat = float(item.get("lat"))
                lng = float(item.get("lon"))
            except (TypeError, ValueError):
                continue
            address = item.get("address") or {}
            namedetails = item.get("namedetails") or {}
            display_name = (
                namedetails.get("name")
                or item.get("name")
                or item.get("display_name")
                or "OpenStreetMap location"
            )
            candidates.append({
                "display_name": display_name,
                "address": item.get("display_name") or display_name,
                "latitude": lat,
                "longitude": lng,
                "confidence": item.get("importance"),
                "source": "nominatim_geocoded",
                "country": address.get("country"),
                "region": address.get("state") or address.get("county") or address.get("city"),
            })

        self.nominatim_cache[cache_key] = candidates[:limit]
        self._cache_set("nominatim", normalized_query, country_hint, category_hint, self.nominatim_cache[cache_key])
        return self.nominatim_cache[cache_key]

    def _merge_geocode_candidates(
        self,
        primary: List[Dict[str, Any]],
        fallback: List[Dict[str, Any]],
        limit: int,
    ) -> List[Dict[str, Any]]:
        merged: List[Dict[str, Any]] = []
        seen = set()
        for candidate in [*(fallback or []), *(primary or [])]:
            try:
                key = (round(float(candidate.get("latitude")), 5), round(float(candidate.get("longitude")), 5))
            except (TypeError, ValueError):
                continue
            if key in seen:
                continue
            if any(_candidate_is_duplicate(candidate, existing) for existing in merged):
                continue
            seen.add(key)
            merged.append(candidate)
            if len(merged) >= limit:
                break
        return merged

    def route_minutes(
        self,
        from_coord: Tuple[float, float],
        to_coord: Tuple[float, float],
        transport_mode: str = "driving-car",
        time_bucket: Optional[str] = None,
    ) -> int:
        if not self.has_api_key():
            raise MissingORSApiKey("OPENROUTESERVICE_API_KEY is not configured")
        key = (
            round(from_coord[0], 5),
            round(from_coord[1], 5),
            round(to_coord[0], 5),
            round(to_coord[1], 5),
            transport_mode,
            time_bucket,
        )
        if key in self.route_cache:
            self.route_stats["route_cache_hits"] = int(self.route_stats.get("route_cache_hits") or 0) + 1
            return self.route_cache[key]

        persisted_minutes = database.get_route_cache(
            key[0],
            key[1],
            key[2],
            key[3],
            transport_mode=transport_mode,
            time_bucket=time_bucket,
        )
        if persisted_minutes is not None:
            minutes = max(1, int(persisted_minutes))
            self.route_cache[key] = minutes
            self.route_stats["route_cache_hits"] = int(self.route_stats.get("route_cache_hits") or 0) + 1
            self.route_stats["route_persistent_cache_hits"] = int(self.route_stats.get("route_persistent_cache_hits") or 0) + 1
            return minutes

        self.route_stats["route_cache_misses"] = int(self.route_stats.get("route_cache_misses") or 0) + 1

        body = {
            "coordinates": [
                [from_coord[1], from_coord[0]],
                [to_coord[1], to_coord[0]],
            ],
            "instructions": False,
        }
        fetch_started = time.perf_counter()
        self.route_stats["route_api_calls"] = int(self.route_stats.get("route_api_calls") or 0) + 1
        try:
            response = requests.post(
                f"{ORS_BASE_URL}/v2/directions/{transport_mode}/geojson",
                headers={"Authorization": self.api_key, "Content-Type": "application/json"},
                json=body,
                timeout=15,
            )
        finally:
            self.route_stats["route_fetch_seconds"] = float(self.route_stats.get("route_fetch_seconds") or 0.0) + (
                time.perf_counter() - fetch_started
            )
        response.raise_for_status()
        payload = response.json()
        duration_seconds = None
        features = payload.get("features") or []
        if features:
            duration_seconds = ((features[0].get("properties") or {}).get("summary") or {}).get("duration")
        if duration_seconds is None:
            routes = payload.get("routes") or []
            if routes:
                duration_seconds = (routes[0].get("summary") or {}).get("duration")
        if duration_seconds is None:
            raise TravelServiceError("ORS response did not include a route duration")
        minutes = max(1, int(round(float(duration_seconds) / 60.0)))
        self.route_cache[key] = minutes
        database.save_route_cache(
            key[0],
            key[1],
            key[2],
            key[3],
            minutes,
            transport_mode=transport_mode,
            time_bucket=time_bucket,
            ttl_days=self.route_cache_ttl_days,
        )
        return minutes


def coordinate_from_saved_location(location: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    return _coord_from_location(location)
