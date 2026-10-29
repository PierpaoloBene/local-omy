import 'package:flutter_test/flutter_test.dart';
import 'package:local_omy/api/models.dart';

Voice voice({String? person, String? source, bool unknown = false}) => Voice.fromJson({
  'label': 'SPEAKER_00',
  'name': 'Persona 1',
  'sample': '',
  'person': person,
  'kind': null,
  'app': null,
  'source': source,
  'pinned': false,
  'unknown': unknown,
  'seconds': 12,
});

void main() {
  test('to label: nobody yet, not a video, not unknown', () {
    expect(voice().todo, isTrue);
    expect(voice(person: 'p1').todo, isFalse);
    expect(voice(source: 'media').todo, isFalse);
    expect(voice(unknown: true).todo, isFalse);
  });

  test('a Mac that does not send unknown yet', () {
    final j = {'label': 'SPEAKER_00', 'name': 'x', 'sample': '', 'pinned': false, 'seconds': 1};
    expect(Voice.fromJson(j).unknown, isFalse);
  });

  test('people come with their index', () {
    final p = People.fromJson({
      'me': {'name': 'Io', 'enrolled': true, 'minutes': 1, 'sessions': 1, 'last_seen': null},
      'people': [],
      'index': {
        'p1': {
          'types': ['room'],
          'places': ['Casa'],
          'conversations': 3,
        },
      },
    });
    expect(p.index['p1']!.places, ['Casa']);
  });
}
