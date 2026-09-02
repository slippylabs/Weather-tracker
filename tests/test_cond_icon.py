"""The condition icon is picked from FREE TEXT, so it needs a corpus, not spot checks.

`summary[date]["cond"]` is whichever human-written phrase the highest-priority
source supplied, so its vocabulary is the union of four providers with no shared
schema. The failure mode is silent: an unmatched phrase falls through to the
thermometer and the strip just looks broken for that one condition. So the bar
here is that EVERY phrase in the corpus reaches a specific icon.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import collect
from collect import cond_icon, WMO_CODES, _COND_FALLBACK

# Every distinct phrase found in the live cache on 2026-09-01, including the
# AccuWeather prose that matches no code table anywhere.
OBSERVED = [
    "Partly Cloudy", "Mostly Sunny", "Sunny", "Mostly Cloudy", "PM Thunderstorms",
    "Cloudy", "Showers", "AM Clouds/PM Sun", "PM Showers", "Thunderstorms",
    "AM Showers", "Drizzle", "Isolated Thunderstorms", "Rain",
    "Hot with plenty of sun", "Scattered Strong Storms", "AM Rain",
    "Sun breaking through clouds at times", "Sunshine and seasonably warm",
    "Scattered Thunderstorms", "Partly sunny", "Strong Storms", "Rain Early",
]

# NWS shortForecast is compositional and is the richest source of phrasing.
NWS = [
    "Sunny", "Mostly Sunny", "Partly Sunny", "Mostly Cloudy", "Cloudy",
    "Clear", "Mostly Clear", "Partly Cloudy",
    "Chance Rain Showers", "Slight Chance Rain Showers", "Likely Rain Showers",
    "Rain Showers Likely", "Chance Showers And Thunderstorms",
    "Showers And Thunderstorms Likely", "Isolated Rain Showers",
    "Scattered Rain Showers", "Patchy Fog", "Areas Of Fog", "Patchy Drizzle",
    "Chance Light Snow", "Snow Likely", "Wintry Mix", "Chance Freezing Rain",
    "Blowing Snow", "Breezy", "Windy", "Haze", "Smoke",
    "Mostly Sunny then Slight Chance Rain Showers",
    "Chance Rain Showers then Mostly Cloudy",
    "Partly Cloudy then Slight Chance Showers And Thunderstorms",
    "Sunny then Chance Showers And Thunderstorms",
]

# weather.com wxPhraseLong and AccuWeather headline styles.
TWC_ACCU = [
    "Sunshine", "Plenty of sunshine", "Abundant sunshine", "Mostly sunny",
    "Intervals of clouds and sunshine", "Clouds and sun", "Times of clouds and sun",
    "Cloudy skies", "Considerable cloudiness", "Overcast", "Low clouds",
    "A shower or two", "A couple of showers", "Periods of rain",
    "A thunderstorm in spots", "A stray afternoon t-storm", "Heavy rain",
    "Snow showers", "A bit of snow", "Flurries", "Ice", "Sleet",
    "A mix of rain and snow", "Areas of low clouds", "Patchy clouds",
    "Warm with sunshine", "Cooler with clouds", "Windy with rain",
    "Fog early", "Hazy sunshine", "Rain Early", "Showers Late",
]

CORPUS = OBSERVED + NWS + TWC_ACCU + sorted(set(WMO_CODES.values()))


@pytest.mark.parametrize("phrase", CORPUS)
@pytest.mark.parametrize("is_day", [True, False])
def test_every_phrase_gets_a_real_icon(phrase, is_day):
    """The fallback is a failure, not a default."""
    icon = cond_icon(phrase, is_day)
    assert icon not in _COND_FALLBACK, (
        f"{phrase!r} fell through to the fallback icon; add a keyword for it"
    )


# Hand-checked classifications. These are the ones a keyword matcher gets wrong
# if the rule order is disturbed.
EXPECTED = {
    # severity wins over the cloud/sun words in the same phrase
    "Chance Showers And Thunderstorms": "⛈️",
    "Sunny then Chance Showers And Thunderstorms": "☀️",   # headline is the first clause
    "Mostly Sunny then Slight Chance Rain Showers": "🌤️",
    "Chance Rain Showers then Mostly Cloudy": "🌧️",
    "A mix of rain and snow": "🌨️",                        # snow outranks rain
    "Chance Freezing Rain": "🧊",                           # ice outranks rain
    "Windy with rain": "🌧️",                                # rain outranks wind
    # cloud amount must not collapse
    "Sunny": "☀️",
    "Mostly Sunny": "🌤️",
    "Partly Cloudy": "⛅",
    "Mostly Cloudy": "☁️",
    "Cloudy": "☁️",
    "Overcast": "☁️",
    # weather.com writes this as one partly-cloudy idea, not two clauses
    "AM Clouds/PM Sun": "⛅",
    # AccuWeather prose that matches no table
    "Hot with plenty of sun": "🌤️",
    "Sun breaking through clouds at times": "⛅",
    "Sunshine and seasonably warm": "☀️",
    "Patchy Fog": "🌫️",
    "Hazy sunshine": "🌫️",
}


@pytest.mark.parametrize("phrase,icon", sorted(EXPECTED.items()))
def test_expected_classification(phrase, icon):
    assert cond_icon(phrase, True) == icon


def test_night_swaps_sun_for_moon():
    assert cond_icon("Sunny", True) == "☀️"
    assert cond_icon("Sunny", False) == "🌙"
    assert cond_icon("Clear", False) == "🌙"
    assert cond_icon("Mostly Sunny", False) == "🌙"
    # Anything that is not a sun/clear condition looks the same after dark.
    for p in ("Rain", "Snow", "Thunderstorms", "Fog", "Cloudy"):
        assert cond_icon(p, True) == cond_icon(p, False), p


def test_missing_or_junk_input_is_safe():
    for junk in (None, "", "   ", "\U0001f4a9", "%%%", 12345):
        assert cond_icon(junk, True) in _COND_FALLBACK or isinstance(cond_icon(junk, True), str)
    assert cond_icon(None, True) == _COND_FALLBACK[0]
    assert cond_icon("", False) == _COND_FALLBACK[1]


def test_every_wmo_label_is_covered():
    """WMO_CODES is the one vocabulary we control; none of it may be unmatched."""
    for label in set(WMO_CODES.values()):
        assert cond_icon(label, True) not in _COND_FALLBACK, label


# Substring matching is what makes "sun" catch "sunny" and "sunshine", so the
# traps it creates have to be pinned down. Both of these are handled by rule
# ORDER rather than by the keywords themselves, which is exactly the kind of
# thing that breaks silently when someone reorders the table.
@pytest.mark.parametrize("phrase,icon", [
    ("Fairly cloudy", "☁️"),          # "fair" is a sun keyword; "cloud" is tested first
    ("Rain and fog", "🌧️"),           # rain outranks fog
    ("Foggy then Sunny", "🌫️"),       # headline clause only
    ("Ice", "🧊"),
    ("Ice Pellets", "🧊"),
    ("Patchy Ice Fog", "🧊"),         # ice outranks fog
    ("Snow and Rain", "🌨️"),
    ("Thunderstorms and Snow", "⛈️"),  # storm is the most severe, tested first
])
def test_substring_and_ordering_traps(phrase, icon):
    assert cond_icon(phrase, True) == icon
