/// Mirrors of the Mac's JSON (omy transcripts / people / speakers / places).
library;

typedef Json = Map<String, dynamic>;

List<T> _list<T>(Object? v, T Function(Json) f) => [for (final x in (v as List? ?? [])) f(x as Json)];

class Speaker {
  final String name;
  final String? person;
  final double minutes;
  Speaker.fromJson(Json j) : name = j['name'], person = j['person'], minutes = (j['minutes'] as num).toDouble();
  bool get isMe => person == 'me';
}

class DaySummary {
  final String day;
  final int minutes, sessions, paragraphs;
  final String? from, to;
  final List<Speaker> speakers;
  final List<String> places;
  final bool unnamedPlaces;
  DaySummary.fromJson(Json j, [String? day])
    : day = day ?? j['day'],
      minutes = j['minutes'],
      sessions = j['sessions'],
      paragraphs = j['paragraphs'],
      from = j['from'],
      to = j['to'],
      speakers = _list(j['speakers'], Speaker.fromJson),
      places = List<String>.from(j['places']),
      unnamedPlaces = j['unnamed_places'];
}

class Progress {
  final bool running;
  final int sessionsTotal, sessionsDone;
  final double minutesTotal, minutesDone;
  Progress.fromJson(Json j)
    : running = j['running'] == true,
      sessionsTotal = j['sessions_total'] ?? 0,
      sessionsDone = j['sessions_done'] ?? 0,
      minutesTotal = (j['minutes_total'] as num? ?? 0).toDouble(),
      minutesDone = (j['minutes_done'] as num? ?? 0).toDouble();
  double? get fraction => minutesTotal > 0 ? (minutesDone / minutesTotal).clamp(0, 1) : null;
}

class Pending {
  final int sessions, openSessions;
  final double minutes;
  final Progress progress;
  Pending.fromJson(Json j)
    : sessions = j['sessions'],
      openSessions = j['open_sessions'],
      minutes = (j['minutes'] as num).toDouble(),
      progress = Progress.fromJson(j['progress'] as Json);
}

class DayList {
  final List<DaySummary> days;
  final Pending pending;
  DayList.fromJson(Json j) : days = _list(j['days'], DaySummary.fromJson), pending = Pending.fromJson(j['pending']);
}

class Place {
  final String? name;
  final double lat, lon, acc;
  Place.fromJson(Json j)
    : name = j['name'],
      lat = (j['lat'] as num).toDouble(),
      lon = (j['lon'] as num).toDouble(),
      acc = (j['acc'] as num).toDouble();
  String get label => name ?? '${lat.toStringAsFixed(4)}, ${lon.toStringAsFixed(4)}';
}

/// One paragraph of transcripts/DAY.jsonl (mac/README.md, "Output per altre app").
class Paragraph {
  final String id, session, start, end, text;
  final String? speaker, person, language, day;
  final ({String kind, String? app})? via;
  final Place? place;
  Paragraph.fromJson(Json j)
    : id = j['id'],
      day = j['day'],
      session = j['session'],
      start = j['start'],
      end = j['end'],
      text = j['text'],
      speaker = j['speaker'],
      person = j['person'],
      language = j['language'],
      via = j['via'] == null ? null : (kind: j['via']['kind'] as String, app: j['via']['app'] as String?),
      place = j['place'] == null ? null : Place.fromJson(j['place']);
  bool get isMe => person == 'me';
}

class Day {
  final DaySummary summary;
  final List<Paragraph> paragraphs;
  Day.fromJson(Json j)
    : summary = DaySummary.fromJson(j['summary'], j['day']),
      paragraphs = _list(j['records'], Paragraph.fromJson);
}

class SearchResult {
  final List<Paragraph> results;
  final bool more;
  SearchResult.fromJson(Json j) : results = _list(j['results'], Paragraph.fromJson), more = j['more'];
}

class Me {
  final String name;
  final bool enrolled;
  final double minutes;
  final int sessions;
  final String? lastSeen;
  Me.fromJson(Json j)
    : name = j['name'],
      enrolled = j['enrolled'],
      minutes = (j['minutes'] as num).toDouble(),
      sessions = j['sessions'],
      lastSeen = j['last_seen'];
}

class Person {
  final String id, name;
  final int samples, sessions;
  final double minutes;
  final String? lastSeen;
  Person.fromJson(Json j)
    : id = j['id'],
      name = j['name'],
      samples = j['samples'],
      sessions = j['sessions'],
      minutes = (j['minutes'] as num).toDouble(),
      lastSeen = j['last_seen'];
}

class People {
  final Me me;
  final List<Person> people;
  People.fromJson(Json j) : me = Me.fromJson(j['me']), people = _list(j['people'], Person.fromJson);
}

class Voice {
  final String label, name, sample;
  final String? person, kind;
  final bool pinned;
  final double seconds;
  Voice.fromJson(Json j)
    : label = j['label'],
      name = j['name'],
      sample = j['sample'],
      person = j['person'],
      kind = j['kind'],
      pinned = j['pinned'],
      seconds = (j['seconds'] as num).toDouble();
}

class VoiceSession {
  final String session;
  final String? start;
  final List<Voice> voices;
  VoiceSession.fromJson(Json j)
    : session = j['session'],
      start = j['start'],
      voices = _list(j['voices'], Voice.fromJson);
}

class DayVoices {
  final String day;
  final List<VoiceSession> sessions;
  DayVoices.fromJson(Json j) : day = j['day'], sessions = _list(j['sessions'], VoiceSession.fromJson);
}

class NamedPlace {
  final String name;
  final double lat, lon;
  final int radius, minutes;
  final String? lastSeen;
  NamedPlace.fromJson(Json j)
    : name = j['name'],
      lat = (j['lat'] as num).toDouble(),
      lon = (j['lon'] as num).toDouble(),
      radius = j['radius'],
      minutes = j['minutes'],
      lastSeen = j['last_seen'];
}

class Spot {
  final double lat, lon;
  final int minutes, days;
  final String? lastSeen;
  final String from, to, sample;
  final ({String name, int meters})? nearest;
  Spot.fromJson(Json j)
    : lat = (j['lat'] as num).toDouble(),
      lon = (j['lon'] as num).toDouble(),
      minutes = j['minutes'],
      days = j['days'],
      lastSeen = j['last_seen'],
      from = j['from'],
      to = j['to'],
      sample = j['sample'],
      nearest = j['nearest'] == null
          ? null
          : (name: j['nearest']['name'] as String, meters: j['nearest']['meters'] as int);
}

class Places {
  final List<NamedPlace> places;
  final List<Spot> unnamed;
  Places.fromJson(Json j)
    : places = _list(j['places'], NamedPlace.fromJson),
      unnamed = _list(j['unnamed'], Spot.fromJson);
}
