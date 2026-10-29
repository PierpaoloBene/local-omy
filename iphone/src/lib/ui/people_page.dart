import 'dart:async';

import 'package:flutter/cupertino.dart';

import '../api/mac_api.dart';
import '../api/models.dart';
import '../features.dart';
import 'app_shell.dart';
import 'common.dart';
import 'conversations.dart';
import 'person_page.dart';
import '../l10n.dart';

/// "Persone": two views. The people (you and those you named), filtered by
/// the kind of conversation, each opening their conversations; and the
/// voices still to label of the last [todoHours], while you remember who
/// spoke (older ones: on the Mac).
/// Every change runs on the Mac and rewrites the days where they speak.
class PeoplePage extends StatefulWidget {
  final MacApi api;
  final Features features;
  final TabShown shown;
  const PeoplePage({super.key, required this.api, required this.features, required this.shown});

  @override
  State<PeoplePage> createState() => _PeoplePageState();
}

const todoHours = 24;

class _PeoplePageState extends State<PeoplePage> {
  People? _people;
  ConversationList? _convs;
  List<VoiceSession> _todo = [];
  String? _type;
  bool _labeling = false;
  Object? _error;
  bool _busy = false;
  String? _playing;
  Timer? _playTimer;

  @override
  void initState() {
    super.initState();
    widget.shown.addListener(_load);
    _load();
  }

  @override
  void dispose() {
    widget.shown.removeListener(_load);
    _playTimer?.cancel();
    if (_playing != null) widget.api.stopVoice();
    super.dispose();
  }

  Future<void> _load() async {
    setState(() => _error = null);
    try {
      final people = await widget.api.people();
      final convs = await widget.api.conversations(type: _type);
      final todo = (await widget.api.unlabeled(hours: todoHours)).sessions;
      if (!mounted) return;
      setState(() {
        _people = people;
        _convs = convs;
        _todo = todo;
      });
    } on MacError catch (e) {
      if (mounted) setState(() => _error = e);
    }
  }

  Future<void> _setType(String? t) async {
    setState(() => _type = t);
    try {
      final c = await widget.api.conversations(type: t);
      if (mounted && _type == t) setState(() => _convs = c);
    } on MacError catch (e) {
      if (mounted) showMessage(context, e, title: tr('Non riuscito', 'Failed'));
    }
  }

  Future<void> _change(Future<void> Function() body) async {
    if (await change(context, (b) => setState(() => _busy = b), body)) await _load();
  }

  Future<void> _play(VoiceSession s, Voice v) async {
    final key = '${s.session}/${v.label}';
    _playTimer?.cancel();
    if (_playing == key) {
      await widget.api.stopVoice();
      setState(() => _playing = null);
      return;
    }
    setState(() => _playing = key);
    try {
      final seconds = await widget.api.playVoice(s.session, v.label);
      _playTimer = Timer(Duration(milliseconds: (seconds * 1000).round() + 200), () {
        if (mounted) setState(() => _playing = null);
      });
    } on Exception catch (e) {
      if (mounted) {
        setState(() => _playing = null);
        showMessage(context, e, title: tr('Ascolto non riuscito', 'Could not play'));
      }
    }
  }

  Future<void> _labelVoice(VoiceSession s, Voice v) async {
    final people = _people;
    final r = await showCupertinoSheet<_Label>(
      context: context,
      scrollableBuilder: (context, controller) => _VoiceSheet(
        controller: controller,
        voice: v,
        prints: widget.features.on('voice_prints'),
        me: people?.me.name,
        known: [...?people?.people.map((p) => p.name)],
      ),
    );
    if (r == null) return;
    if (r.unknown) return _change(() => widget.api.setVoiceUnknown(s.session, [v.label]));
    final (:type, :name, unknown: _) = r;
    final rename = type != 'media' && name.isNotEmpty && !(name == v.name && v.pinned);
    if (type == v.type && !rename) return;
    await _change(() async {
      if (type != v.type) await widget.api.setVoiceKind(s.session, [v.label], type);
      if (rename) await widget.api.nameVoice(s.session, v.label, name);
    });
  }

  /// The voices to label of a session, all at once: strangers at the next
  /// table, a video on, a meeting.
  Future<void> _labelSession(VoiceSession s) async {
    final unnamed = [
      for (final v in s.voices)
        if (v.todo) v.label,
    ];
    if (unnamed.isEmpty) return;
    final type = await chooseAction(
      context,
      title: unnamed.length == 1
          ? tr('Segna la voce come', 'Mark the voice as')
          : tr('Segna le ${unnamed.length} voci come', 'Mark the ${unnamed.length} voices as'),
      actions: [
        ('unknown', tr('Sconosciuti', 'Unknown'), false),
        ('media', 'Video', false),
        ('meeting', tr('Riunione online', 'Online meeting'), false),
        ('room', tr('Di persona', 'In person'), false),
        ('auto', tr('Automatico', 'Automatic'), false),
      ],
    );
    if (type == null) return;
    await _change(
      () => type == 'unknown'
          ? widget.api.setVoiceUnknown(s.session, unnamed)
          : widget.api.setVoiceKind(s.session, unnamed, type),
    );
  }

  Future<void> _open({
    required String id,
    required String name,
    required double minutes,
    required int sessions,
    String? lastSeen,
  }) async {
    final changed = await Navigator.of(context).push<bool>(
      CupertinoPageRoute(
        builder: (_) => PersonPage(
          api: widget.api,
          person: id,
          name: name,
          minutes: minutes,
          sessions: sessions,
          lastSeen: lastSeen,
          index: _people?.index[id],
          others: _people?.people ?? [],
        ),
      ),
    );
    if (changed == true) await _load();
  }

  @override
  Widget build(BuildContext context) {
    final people = _people;
    final todo = _todo.fold(0, (n, s) => n + s.voices.where((v) => v.todo).length);
    return TabScaffold(
      title: tr('Persone', 'People'),
      scrollToTop: widget.shown.retap,
      onRefresh: _load,
      busy: _busy,
      slivers: people == null
          ? [LoadState(loading: _error == null, error: _error, onRetry: _load)]
          : [
              SliverList.list(
                children: [
                  if (_error != null) Stale(_error!),
                  if (!widget.features.on('voice_prints'))
                    Notice(
                      icon: CupertinoIcons.info_circle_fill,
                      text: tr(
                        'Impronte vocali disattivate: le voci nuove non vengono riconosciute, e non si '
                            'possono dare nomi (Impostazioni, Funzioni).',
                        'Voice prints are off: new voices are not recognized, and cannot be named '
                            '(Settings, Features).',
                      ),
                    ),
                  Padding(
                    padding: const EdgeInsets.fromLTRB(16, 0, 16, 16),
                    child: CupertinoSlidingSegmentedControl<bool>(
                      groupValue: _labeling,
                      onValueChanged: (x) => setState(() => _labeling = x ?? _labeling),
                      children: {
                        false: Text(tr('Persone', 'People')),
                        true: Text('${tr('Da etichettare', 'To label')}${todo > 0 ? ' · $todo' : ''}'),
                      },
                    ),
                  ),
                  if (_labeling) ..._voiceSections(context) else ..._peopleView(context, people),
                ],
              ),
            ],
    );
  }

  List<Widget> _peopleView(BuildContext context, People people) {
    final index = people.index;
    final shown = [
      for (final p in people.people)
        if (_type == null || (index[p.id]?.types.contains(_type) ?? false)) p,
    ];
    final label = conversationTypes
        .firstWhere((t) => t.$1 == _type, orElse: () => ('', tr('Tutte', 'All'), CupertinoIcons.circle))
        .$2;
    return [
      FilterPills<String>(values: conversationTypes, selected: _type, onChanged: _setType),
      const SizedBox(height: 16),
      if (_type == null) ...[_meCard(context, people.me), const SizedBox(height: 20)],
      if (_type != 'media')
        Section(
          header: _type == null
              ? tr('Persone note · ${people.people.length}', 'Known people · ${people.people.length}')
              : tr('Persone · $label', 'People · $label'),
          children: shown.isEmpty
              ? [
                  IosRow(
                    title: _type == null
                        ? tr('Nessuna ancora', 'None yet')
                        : tr('Nessuna persona con un nome', 'No named people'),
                    subtitle: tr('Dai un nome alle voci in «Da etichettare».', 'Name the voices in “To label”.'),
                  ),
                ]
              : [for (final p in shown) _personRow(context, p)],
        ),
      if (_convs != null)
        ConversationSection(
          api: widget.api,
          list: _convs!,
          previousPageTitle: tr('Persone', 'People'),
          header: _type == null
              ? tr('Conversazioni recenti', 'Recent conversations')
              : tr('Conversazioni · $label', 'Conversations · $label'),
          limit: 8,
          onMore: () => Navigator.of(context).push(
            CupertinoPageRoute(
              builder: (_) => ConversationsPage(
                api: widget.api,
                title: _type == null ? tr('Conversazioni', 'Conversations') : label,
                type: _type,
                previousPageTitle: tr('Persone', 'People'),
              ),
            ),
          ),
        ),
    ];
  }

  Widget _meCard(BuildContext context, Me me) => InsetCard(
    onTap: () => _open(id: 'me', name: me.name, minutes: me.minutes, sessions: me.sessions, lastSeen: me.lastSeen),
    child: Row(
      children: [
        PersonAvatar(name: me.name, me: true, size: 58),
        const SizedBox(width: 14),
        Expanded(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(me.name, style: Ios.title(context)),
              const SizedBox(height: 2),
              Text(
                me.enrolled
                    ? '${duration(me.minutes)} · ${counted(me.sessions, 'sessione', 'sessioni', 'session', 'sessions')}'
                          '${me.lastSeen == null ? '' : ' · ${tr('ultima', 'last')} ${shortDay(me.lastSeen!)}'}'
                    : tr(
                        'Profilo vocale non ancora creato (sul Mac: omy voice enroll)',
                        'No voice profile yet (on the Mac: omy voice enroll)',
                      ),
                style: Ios.footnote(context),
              ),
            ],
          ),
        ),
      ],
    ),
  );

  Widget _personRow(BuildContext context, Person p) {
    final places = _people?.index[p.id]?.places ?? [];
    return IosRow(
      leading: PersonAvatar(name: p.name),
      title: p.name,
      subtitle: [
        duration(p.minutes),
        counted(p.sessions, 'sessione', 'sessioni', 'session', 'sessions'),
        if (p.lastSeen != null) '${tr('ultima', 'last')} ${shortDay(p.lastSeen!)}',
        if (places.isNotEmpty) places.first,
      ].join(' · '),
      trailing: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          for (final t in _people?.index[p.id]?.types ?? <String>[])
            Padding(
              padding: const EdgeInsets.only(left: 4),
              child: Icon(typeIcon(t), size: 14, color: resolve(context, CupertinoColors.tertiaryLabel)),
            ),
        ],
      ),
      chevron: true,
      onTap: () => _open(id: p.id, name: p.name, minutes: p.minutes, sessions: p.sessions, lastSeen: p.lastSeen),
    );
  }

  /// The queue: sessions of the last days with voices to label, only those.
  List<Widget> _voiceSections(BuildContext context) {
    final sessions = [
      for (final s in _todo)
        if (s.voices.any((v) => v.todo)) s,
    ];
    if (sessions.isEmpty) {
      return [
        Empty(
          tr(
            'Nessuna voce da etichettare nelle ultime $todoHours ore. Quelle più vecchie si etichettano dal Mac.',
            'No voices to label in the last $todoHours hours. Older ones are labeled on the Mac.',
          ),
        ),
      ];
    }
    return [
      for (final s in sessions)
        Section(
          header: [if (s.day != null) longDay(s.day!), if (s.start != null) _localTime(s.start!)].join(' · '),
          headerTrailing: CupertinoButton(
            padding: EdgeInsets.zero,
            minimumSize: const Size(0, 28),
            onPressed: _busy ? null : () => _labelSession(s),
            child: Text(tr('Segna tutte…', 'Mark all…')),
          ),
          children: [
            for (final voice in s.voices)
              if (voice.todo) _voiceRow(context, s, voice),
          ],
        ),
      Padding(
        padding: const EdgeInsets.fromLTRB(32, 0, 32, 0),
        child: Empty(
          tr(
            'Le voci senza nome delle ultime $todoHours ore, finché ricordi chi parlava. '
                'Quelle più vecchie si etichettano dal Mac.',
            'The unnamed voices of the last $todoHours hours, while you remember who was speaking. '
                'Older ones are labeled on the Mac.',
          ),
        ),
      ),
    ];
  }

  Widget _voiceRow(BuildContext context, VoiceSession s, Voice v) {
    final playing = _playing == '${s.session}/${v.label}';
    final known = v.person != null;
    final t = resolve(context, tint);
    return IosRow(
      leading: GestureDetector(
        onTap: () => _play(s, v),
        behavior: HitTestBehavior.opaque,
        child: Container(
          width: 36,
          height: 36,
          decoration: BoxDecoration(
            shape: BoxShape.circle,
            color: t.withValues(alpha: playing ? 1 : 0.14),
          ),
          child: Icon(
            playing ? CupertinoIcons.stop_fill : CupertinoIcons.play_fill,
            size: 16,
            color: playing ? const Color(0xFFFFFFFF) : t,
          ),
        ),
      ),
      title: v.name,
      titleStyle: Ios.headline(
        context,
      ).copyWith(color: known ? personColor(context, v.name, me: v.person == 'me') : null),
      subtitle: [
        voiceTypeLabel(v),
        v.seconds < 60 ? '${v.seconds.round()} s' : duration(v.seconds / 60),
        if (v.pinned)
          tr('nominata da te', 'named by you')
        else if (v.person != null)
          tr('riconosciuta', 'recognized')
        else if (v.unknown)
          tr('sconosciuta', 'unknown'),
      ].join(' · '),
      below: v.sample.isEmpty
          ? null
          : Padding(
              padding: const EdgeInsets.only(top: 3),
              child: Text(
                '«${v.sample}»',
                maxLines: 3,
                overflow: TextOverflow.ellipsis,
                style: Ios.footnote(context).copyWith(fontStyle: FontStyle.italic),
              ),
            ),
      trailing: CupertinoButton(
        padding: EdgeInsets.zero,
        minimumSize: const Size(44, 44),
        onPressed: _busy ? null : () => _labelVoice(s, v),
        child: Text(v.todo ? tr('Etichetta', 'Label') : tr('Cambia', 'Change')),
      ),
    );
  }

  static String _localTime(String iso) {
    final t = DateTime.parse(iso).toLocal();
    return '${t.hour.toString().padLeft(2, '0')}:${t.minute.toString().padLeft(2, '0')}';
  }
}

IconData voiceTypeIcon(Voice v) => switch (v.type) {
  'media' => CupertinoIcons.play_rectangle_fill,
  'meeting' => v.app == 'iPhone' ? CupertinoIcons.phone_fill : CupertinoIcons.videocam_fill,
  _ => CupertinoIcons.person_fill,
};

/// The app the Mac puts on a meeting it didn't see, in either language.
bool _anyMeeting(String? app) => app == null || app == 'Riunione' || app == 'Meeting';

String voiceTypeLabel(Voice v) => switch (v.type) {
  'media' => v.app == null || v.app == 'Video' ? 'Video' : 'Video · ${v.app}',
  'meeting' =>
    v.app == 'iPhone'
        ? tr('Chiamata', 'Call')
        : _anyMeeting(v.app)
        ? tr('Riunione online', 'Online meeting')
        : tr('Riunione · ${v.app}', 'Meeting · ${v.app}'),
  _ => tr('Di persona', 'In person'),
};

typedef _Label = ({String type, String name, bool unknown});

/// Where a voice comes from and, unless a video, who it is: you first, then
/// the known people, or somebody you don't know.
class _VoiceSheet extends StatefulWidget {
  final ScrollController controller;
  final Voice voice;
  final bool prints;
  final String? me;
  final List<String> known;
  const _VoiceSheet({
    required this.controller,
    required this.voice,
    required this.prints,
    required this.me,
    required this.known,
  });

  @override
  State<_VoiceSheet> createState() => _VoiceSheetState();
}

class _VoiceSheetState extends State<_VoiceSheet> {
  late final _c = TextEditingController(text: widget.voice.person == null ? '' : widget.voice.name);
  late String _type = widget.voice.type;

  @override
  void dispose() {
    _c.dispose();
    super.dispose();
  }

  void _done([String? name]) =>
      Navigator.pop<_Label>(context, (type: _type, name: (name ?? _c.text).trim(), unknown: false));

  void _unknown() => Navigator.pop<_Label>(context, (type: _type, name: '', unknown: true));

  @override
  Widget build(BuildContext context) {
    final v = widget.voice;
    final q = _c.text.trim().toLowerCase();
    final names = [?widget.me, ...widget.known].where((s) => q.isEmpty || s.toLowerCase().contains(q)).take(20);
    final named = _type != 'media' && widget.prints;
    return SheetPage(
      controller: widget.controller,
      title: tr('Chi è?', 'Who is it?'),
      action: tr('Salva', 'Save'),
      onAction: _done,
      children: [
        if (v.sample.isNotEmpty)
          Padding(
            padding: const EdgeInsets.fromLTRB(32, 0, 32, 16),
            child: Text(
              '«${v.sample}»',
              maxLines: 5,
              overflow: TextOverflow.ellipsis,
              style: Ios.subhead(context).copyWith(fontStyle: FontStyle.italic),
            ),
          ),
        Padding(
          padding: const EdgeInsets.fromLTRB(16, 0, 16, 8),
          child: CupertinoSlidingSegmentedControl<String>(
            groupValue: _type,
            onValueChanged: (t) => setState(() => _type = t ?? _type),
            children: {
              'room': _Segment(CupertinoIcons.person_fill, tr('Di persona', 'In person')),
              'meeting': const _Segment(CupertinoIcons.videocam_fill, 'Online'),
              'media': const _Segment(CupertinoIcons.play_rectangle_fill, 'Video'),
            },
          ),
        ),
        Padding(
          padding: const EdgeInsets.fromLTRB(32, 4, 32, 12),
          child: Text(switch (_type) {
            'media' => tr(
              'Una voce da uno schermo o da un altoparlante: nessun nome, nessuna impronta vocale. '
                  'Nelle trascrizioni diventa «Video».',
              'A voice from a screen or a speaker: no name, no voice print. '
                  'In the transcripts it becomes “Video”.',
            ),
            'meeting' =>
              v.kind == 'call' && !_anyMeeting(v.app)
                  ? tr(
                      'Riconosciuta dal Mac: ${v.app}. Puoi dirle chi è.',
                      'Recognized by the Mac: ${v.app}. You can say who it is.',
                    )
                  : tr(
                      'Qualcuno in una chiamata o una riunione online (Teams, Zoom, Meet).',
                      'Someone in a call or an online meeting (Teams, Zoom, Meet).',
                    ),
            _ => tr('Qualcuno nella stanza con te.', 'Someone in the room with you.'),
          }, style: Ios.footnote(context)),
        ),
        if (named) ...[
          CupertinoFormSection.insetGrouped(
            children: [
              CupertinoTextFormFieldRow(
                controller: _c,
                placeholder: tr('Nome (facoltativo)', 'Name (optional)'),
                textCapitalization: TextCapitalization.words,
                onChanged: (_) => setState(() {}),
                onFieldSubmitted: _done,
              ),
            ],
          ),
          Section(
            header: tr('Scegli', 'Choose'),
            children: [
              for (final n in names)
                IosRow(
                  leading: PersonAvatar(name: n, me: n == widget.me, size: 30),
                  title: n,
                  titleStyle: Ios.body(context),
                  onTap: () => _done(n),
                ),
              _unknownRow(context),
            ],
          ),
        ] else if (_type != 'media') ...[
          Empty(
            tr(
              'Impronte vocali disattivate: si può scegliere il tipo, ma non dare un nome.',
              'Voice prints are off: you can choose the type, but not give a name.',
            ),
          ),
          Section(children: [_unknownRow(context)]),
        ],
      ],
    );
  }

  Widget _unknownRow(BuildContext context) => IosRow(
    leading: const IconSquare(CupertinoIcons.question, CupertinoColors.systemGrey),
    title: tr('Sconosciuto', 'Unknown'),
    titleStyle: Ios.body(context),
    subtitle: tr(
      'Non sai chi è: niente nome né impronta, non te lo chiede più',
      'You don\'t know who it is: no name or voice print, not asked again',
    ),
    onTap: _unknown,
  );
}

class _Segment extends StatelessWidget {
  final IconData icon;
  final String text;
  const _Segment(this.icon, this.text);

  @override
  Widget build(BuildContext context) => Padding(
    padding: const EdgeInsets.symmetric(vertical: 6),
    child: Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        Icon(icon, size: 18),
        const SizedBox(height: 2),
        Text(text, style: const TextStyle(fontSize: 12)),
      ],
    ),
  );
}
