import { useEffect, useMemo, useState } from "react";
import "leaflet/dist/leaflet.css";
import L from "leaflet";
import { Check, ChevronDown, Crosshair, Loader2, MapPin, Search } from "lucide-react";
import { MapContainer, Marker, TileLayer, useMap, useMapEvents } from "react-leaflet";
import { geocodeLocation, type GeocodeCandidate, type SavedLocation } from "../services/planService";
import { Button } from "./ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "./ui/dialog";
import { Input } from "./ui/input";

export type MapPoint = {
  lat: number;
  lng: number;
};

type LocationPickerDialogProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title?: string;
  description?: string;
  label?: string;
  initialCenter?: MapPoint | null;
  initialPin?: MapPoint | null;
  candidates?: GeocodeCandidate[];
  savedLocations?: Array<Partial<SavedLocation>>;
  recentLocations?: Array<Partial<SavedLocation>>;
  initialSearchQuery?: string;
  searchCategory?: string;
  confirmLabel?: string;
  saving?: boolean;
  onConfirm: (candidate: GeocodeCandidate) => Promise<void> | void;
};

const FALLBACK_MAP_CENTER: MapPoint = { lat: 2.9264, lng: 101.6412 };
const EMPTY_CANDIDATES: GeocodeCandidate[] = [];
const EMPTY_SAVED_LOCATIONS: Array<Partial<SavedLocation>> = [];

const pinIcon = L.divIcon({
  className: "",
  html: '<div style="width: 22px; height: 22px; border-radius: 9999px; background: #4f6df5; border: 3px solid white; box-shadow: 0 8px 18px rgba(15, 23, 42, 0.35);"></div>',
  iconSize: [22, 22],
  iconAnchor: [11, 11],
});

export function candidateToMapPoint(candidate?: GeocodeCandidate | null): MapPoint | null {
  if (!candidate) return null;
  if (!Number.isFinite(candidate.latitude) || !Number.isFinite(candidate.longitude)) return null;
  return { lat: Number(candidate.latitude), lng: Number(candidate.longitude) };
}

function savedLocationToCandidate(location: Partial<SavedLocation>): GeocodeCandidate | null {
  const latitude = Number(location.latitude);
  const longitude = Number(location.longitude);
  if (!Number.isFinite(latitude) || !Number.isFinite(longitude)) return null;
  return {
    label: location.label,
    display_name: location.display_name || location.label || location.address || "Saved location",
    address: location.address || location.display_name || location.label || "Saved location",
    latitude,
    longitude,
    source: location.source || "saved_profile",
    confirmed_by_user: location.confirmed_by_user ?? true,
  };
}

export function getDefaultMapCenter(): MapPoint {
  const envLat = Number(import.meta.env.VITE_DEFAULT_MAP_LAT);
  const envLng = Number(import.meta.env.VITE_DEFAULT_MAP_LNG);
  if (Number.isFinite(envLat) && Number.isFinite(envLng)) {
    return { lat: envLat, lng: envLng };
  }
  return FALLBACK_MAP_CENTER;
}

function RecenterMap({ center, open }: { center: MapPoint; open: boolean }) {
  const map = useMap();

  useEffect(() => {
    map.setView([center.lat, center.lng], map.getZoom());
    window.setTimeout(() => map.invalidateSize(), 80);
  }, [center.lat, center.lng, map]);

  useEffect(() => {
    if (!open) return;
    window.setTimeout(() => map.invalidateSize(), 120);
  }, [map, open]);

  return null;
}

function PinPickerMap({
  center,
  open,
  value,
  onChange,
}: {
  center: MapPoint;
  open: boolean;
  value: MapPoint | null;
  onChange: (point: MapPoint) => void;
}) {
  useMapEvents({
    click(event) {
      onChange({ lat: event.latlng.lat, lng: event.latlng.lng });
    },
  });

  return (
    <>
      <RecenterMap center={center} open={open} />
      {value && <Marker position={[value.lat, value.lng]} icon={pinIcon} />}
    </>
  );
}

function firstCandidatePoint(candidates: GeocodeCandidate[] = []): MapPoint | null {
  for (const candidate of candidates) {
    const point = candidateToMapPoint(candidate);
    if (point) return point;
  }
  return null;
}

export function LocationPickerDialog({
  open,
  onOpenChange,
  title = "Pick location on map",
  description,
  label,
  initialCenter,
  initialPin,
  candidates,
  savedLocations,
  recentLocations,
  initialSearchQuery = "",
  searchCategory,
  confirmLabel = "Save this map point",
  saving = false,
  onConfirm,
}: LocationPickerDialogProps) {
  const safeCandidates = candidates || EMPTY_CANDIDATES;
  const safeSavedLocations = savedLocations || EMPTY_SAVED_LOCATIONS;
  const safeRecentLocations = recentLocations || EMPTY_SAVED_LOCATIONS;
  const savedCandidates = useMemo(
    () => safeSavedLocations.map(savedLocationToCandidate).filter(Boolean) as GeocodeCandidate[],
    [safeSavedLocations],
  );
  const recentCandidates = useMemo(
    () => safeRecentLocations.map(savedLocationToCandidate).filter(Boolean) as GeocodeCandidate[],
    [safeRecentLocations],
  );
  const [center, setCenter] = useState<MapPoint>(getDefaultMapCenter());
  const [pin, setPin] = useState<MapPoint | null>(null);
  const [selectedCandidate, setSelectedCandidate] = useState<GeocodeCandidate | null>(null);
  const [referenceLabel, setReferenceLabel] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState(initialSearchQuery);
  const [searchResults, setSearchResults] = useState<GeocodeCandidate[]>([]);
  const [notice, setNotice] = useState<string | null>(null);
  const [isSearching, setIsSearching] = useState(false);
  const [showSavedLocations, setShowSavedLocations] = useState(false);
  const [showRecentLocations, setShowRecentLocations] = useState(false);

  useEffect(() => {
    if (!open) return;
    const candidatePoint = firstCandidatePoint(safeCandidates);
    const savedPoint = firstCandidatePoint(savedCandidates);
    const recentPoint = firstCandidatePoint(recentCandidates);
    const nextCenter = initialPin || initialCenter || candidatePoint || savedPoint || recentPoint || getDefaultMapCenter();
    const initialCandidate = safeCandidates.find(candidateToMapPoint) || null;

    setCenter(nextCenter);
    setPin(initialPin || initialCenter || candidatePoint || nextCenter);
    setSelectedCandidate(initialCandidate);
    setReferenceLabel(initialCandidate?.display_name || initialCandidate?.address || null);
    setSearchResults(safeCandidates);
    setSearchQuery(initialSearchQuery);
    setNotice(null);
    setShowSavedLocations(false);
    setShowRecentLocations(false);
  }, [safeCandidates, initialCenter, initialPin, initialSearchQuery, open, savedCandidates, recentCandidates]);

  const handlePickPoint = (point: MapPoint) => {
    setPin(point);
    setSelectedCandidate(null);
    setNotice(referenceLabel ? `Pin selected near ${referenceLabel}.` : (label ? `Pin selected for ${label}.` : "Pin selected on the map."));
  };

  const handleUseDeviceLocation = () => {
    if (!navigator.geolocation) {
      setNotice("Your browser does not support device location. You can still search or click the map.");
      return;
    }

    setNotice("Requesting your current location...");
    navigator.geolocation.getCurrentPosition(
      (position) => {
        const point = {
          lat: position.coords.latitude,
          lng: position.coords.longitude,
        };
        setCenter(point);
        setPin(point);
        setSelectedCandidate({
          display_name: "Current location pin",
          address: "Selected from browser device location",
          latitude: point.lat,
          longitude: point.lng,
          source: "device_location_pin",
        });
        setReferenceLabel("your current location");
        setNotice("Centered on your current location. Adjust the pin if needed before confirming.");
      },
      () => {
        setNotice("I could not access your current location. Search nearby or click the map instead.");
      },
      { enableHighAccuracy: true, timeout: 10000, maximumAge: 60000 },
    );
  };

  const handleSearch = async () => {
    if (!searchQuery.trim()) return;
    setIsSearching(true);
    setNotice(null);
    try {
      const result = await geocodeLocation(searchQuery.trim(), searchCategory);
      const results = result.candidates || [];
      setSearchResults(results);
      if (results.length) {
        const candidate = results[0];
        const point = candidateToMapPoint(candidate);
        if (point) {
          setCenter(point);
          setPin(point);
          setSelectedCandidate(candidate);
          setReferenceLabel(candidate.display_name || candidate.address || null);
        }
      }
      const warning = result.warnings?.join(" ") || result.warning;
      if (warning) {
        setNotice(warning);
      } else if (!results.length) {
        setNotice("No search results found. You can still click the exact point on the map.");
      }
    } catch (error) {
      setNotice("Location search is unavailable right now. You can still click the map manually.");
    } finally {
      setIsSearching(false);
    }
  };

  const handleSelectCandidate = (candidate: GeocodeCandidate) => {
    const point = candidateToMapPoint(candidate);
    if (!point) return;
    setCenter(point);
    setPin(point);
    setSelectedCandidate(candidate);
    setReferenceLabel(candidate.display_name || candidate.address || null);
    setNotice(`Pin moved to ${candidate.display_name || candidate.address || "the selected result"}.`);
  };

  const handleConfirm = async () => {
    if (!pin) return;
    const confirmed = selectedCandidate
      ? {
          ...selectedCandidate,
          latitude: pin.lat,
          longitude: pin.lng,
        }
      : {
          display_name: label ? `${label} map point` : "Pinned map point",
          address: searchQuery.trim() || referenceLabel || "Pinned map point",
          latitude: pin.lat,
          longitude: pin.lng,
          source: "manual_map_pin",
        };

    await onConfirm(confirmed);
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="max-w-3xl rounded-3xl"
        hideCloseButton
        style={{
          position: "fixed",
          left: "50%",
          top: "50%",
          transform: "translate(-50%, -50%)",
          zIndex: 80,
          maxHeight: "92vh",
          overflowY: "auto",
        }}
      >
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>
            {description || `Search nearby, use your current location, or click the exact place for ${label || "this location"}.`}
          </DialogDescription>
        </DialogHeader>

        {savedCandidates.length > 0 && (
          <div className="rounded-2xl border border-border bg-secondary/10">
            <button
              type="button"
              className="flex w-full items-center justify-between gap-2 px-3 py-2 text-left text-xs font-medium"
              onClick={() => setShowSavedLocations(value => !value)}
            >
              <span>Saved locations ({savedCandidates.length})</span>
              <ChevronDown className={`h-4 w-4 transition-transform ${showSavedLocations ? "rotate-180" : ""}`} />
            </button>
            {showSavedLocations && (
              <div className="max-h-32 space-y-1 overflow-y-auto border-t border-border/70 p-2">
                {savedCandidates.map((candidate, index) => (
                  <button
                    type="button"
                    key={`${candidate.label || candidate.display_name}-${candidate.latitude}-${candidate.longitude}-${index}`}
                    className="flex w-full items-start gap-2 rounded-xl px-2 py-1.5 text-left text-xs hover:bg-background"
                    onClick={() => handleSelectCandidate(candidate)}
                  >
                    <MapPin className="mt-0.5 h-3.5 w-3.5 shrink-0 text-primary" />
                    <span className="min-w-0">
                      <span className="block truncate font-medium text-foreground">
                        {candidate.label || candidate.display_name || "Saved location"}
                      </span>
                      <span className="block truncate text-muted-foreground">
                        {candidate.address || candidate.display_name || "Confirmed saved place"}
                      </span>
                    </span>
                  </button>
                ))}
              </div>
            )}
          </div>
        )}

        {recentCandidates.length > 0 && (
          <div className="rounded-2xl border border-border bg-secondary/10">
            <button
              type="button"
              className="flex w-full items-center justify-between gap-2 px-3 py-2 text-left text-xs font-medium"
              onClick={() => setShowRecentLocations(value => !value)}
            >
              <span>Recent locations ({recentCandidates.length})</span>
              <ChevronDown className={`h-4 w-4 transition-transform ${showRecentLocations ? "rotate-180" : ""}`} />
            </button>
            {showRecentLocations && (
              <div className="max-h-32 space-y-1 overflow-y-auto border-t border-border/70 p-2">
                {recentCandidates.map((candidate, index) => (
                  <button
                    type="button"
                    key={`${candidate.label || candidate.display_name}-${candidate.latitude}-${candidate.longitude}-${index}`}
                    className="flex w-full items-start gap-2 rounded-xl px-2 py-1.5 text-left text-xs hover:bg-background"
                    onClick={() => handleSelectCandidate({ ...candidate, source: candidate.source || "recent" })}
                  >
                    <MapPin className="mt-0.5 h-3.5 w-3.5 shrink-0 text-primary" />
                    <span className="min-w-0">
                      <span className="block truncate font-medium text-foreground">
                        {candidate.label || candidate.display_name || "Recent location"}
                      </span>
                      <span className="block truncate text-muted-foreground">
                        {candidate.address || candidate.display_name || "Recently used place"}
                      </span>
                    </span>
                  </button>
                ))}
              </div>
            )}
          </div>
        )}

        <div className="rounded-2xl border border-border bg-secondary/10 p-3">
          <p className="mb-2 text-xs font-medium">Search new location</p>
          <div className="flex flex-col gap-2 sm:flex-row">
            <Input
              value={searchQuery}
              onChange={(event) => setSearchQuery(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  event.preventDefault();
                  handleSearch();
                }
              }}
              placeholder="Search a nearby landmark or address"
              className="rounded-xl bg-background"
            />
            <Button
              type="button"
              variant="outline"
              className="rounded-xl bg-background"
              disabled={!searchQuery.trim() || isSearching}
              onClick={handleSearch}
            >
              {isSearching ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Search className="mr-2 h-4 w-4" />}
              Search nearby
            </Button>
            <Button
              type="button"
              variant="outline"
              className="rounded-xl bg-background"
              onClick={handleUseDeviceLocation}
            >
              <Crosshair className="mr-2 h-4 w-4" />
              Use my current location
            </Button>
          </div>
        </div>

        {notice && (
          <div className="rounded-xl border border-primary/15 bg-primary/5 px-3 py-2 text-xs text-muted-foreground">
            {notice}
          </div>
        )}

        {searchResults.length > 0 && (
          <div className="max-h-28 space-y-1 overflow-y-auto rounded-2xl border border-border bg-secondary/10 p-2">
            {searchResults.slice(0, 8).map((candidate, index) => (
              <button
                type="button"
                key={`${candidate.latitude}-${candidate.longitude}-${candidate.display_name || candidate.address || index}`}
                className="flex w-full items-start gap-2 rounded-xl px-2 py-1.5 text-left text-xs hover:bg-background"
                onClick={() => handleSelectCandidate(candidate)}
              >
                <MapPin className="mt-0.5 h-3.5 w-3.5 shrink-0 text-primary" />
                <span>
                  <span className="block font-medium text-foreground">
                    {candidate.display_name || candidate.address || "Location result"}
                  </span>
                  {candidate.address && candidate.address !== candidate.display_name && (
                    <span className="block text-muted-foreground">{candidate.address}</span>
                  )}
                </span>
              </button>
            ))}
          </div>
        )}

        <div className="overflow-hidden rounded-2xl border border-border">
          <MapContainer
            center={[center.lat, center.lng]}
            zoom={16}
            scrollWheelZoom
            className="w-full"
            style={{ height: 420, width: "100%", zIndex: 1 }}
          >
            <TileLayer
              attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
              url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
            />
            <PinPickerMap
              center={center}
              open={open}
              value={pin}
              onChange={handlePickPoint}
            />
          </MapContainer>
        </div>

        <p className="text-xs text-muted-foreground">
          Tip: zoom in and click the building entrance or closest point you want JPlan to use for travel timing.
        </p>

        <DialogFooter>
          <Button
            variant="outline"
            className="rounded-xl"
            onClick={() => onOpenChange(false)}
            disabled={saving}
          >
            Cancel
          </Button>
          <Button
            className="rounded-xl"
            onClick={handleConfirm}
            disabled={!pin || saving}
          >
            {saving ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Check className="mr-2 h-4 w-4" />}
            {confirmLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
