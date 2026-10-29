import 'dart:async';

import 'package:flutter/cupertino.dart';

import '../api/mac_api.dart';
import '../api/models.dart';
import '../features.dart';
import 'app_shell.dart';
import 'common.dart';
import 'conversations.dart';
import 'person_page.dart';

/// "Persone": two views. The people (you and those you named), filtered by
/// the kind of conversation, each opening their conversations; and the
/// voices still to label, of all the last days in one queue.
/// Every change runs on the Mac and rewrites the days where they speak.
class PeoplePage extends StatefulWidget {
  final MacApi api;
  final Features features;
  final TabShown shown;
  const PeoplePage({super.key, required this.api, required this.features, required this.shown});

  @override
  State<PeoplePage> createState() => _PeoplePageState();
}

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
      final todo = (await widget.api.unlabeled()).sessions;
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
      if (mounted) showMessage(context, e, title: 'Non riuscito');
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
        showMessage(context, e, title: 'Ascolto non riuscito');
      }
    }
  }

  Future<void> _labelVoice(VoiceSession s, Voice v) async {
    final people = _people;
    final r = await showCupertinoSheet<(String, String)>(
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
    final (type, name) = r;
    final rename = type != 'media' && name.isNotEmpty && !(name == v.name && v.pinned);
    if (type == v.type && !rename) return;
    await _change(() async {
      if (type != v.type) await widget.api.setVoiceKind(s.session, [v.label], type);
      if (rename) await widget.api.nameVoice(s.session, v.label, name);
    });
  }

  /// The voices without a name of a session, all at once: a video on, a meeting.
  Future<void> _labelSession(VoiceSession s) async {
    final unnamed = [
      for (final v in s.voices)
        if (v.person == null) v.label,
    ];
    if (unnamed.isEmpty) return;
    final type = await chooseAction(
      context,
      title: unnamed.length == 1 ? 'Segna la voce senza nome come' : 'Segna le ${unnamed.length} voci senza nome come',
      actions: [
        ('media', 'Video', false),
        ('meeting', 'Riunione online', false),
        ('room', 'Di persona', false),
        ('auto', 'Automatico', false),
      ],
    );
    if (type != null) await _change(() => widget.api.setVoiceKind(s.session, unnamed, type));
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
          index: _convs?.people[id],
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
      title: 'Persone',
      onRefresh: _load,
      busy: _busy,
      slivers: people == null
          ? [LoadState(loading: _error == null, error: _error, onRetry: _load)]
          : [
              SliverList.list(
                children: [
                  if (!widget.features.on('voice_prints'))
                    const Notice(
                      icon: CupertinoIcons.info_circle_fill,
                      text:
                          'Impronte vocali disattivate: le voci nuove non vengono riconosciute, e non si '
                          'possono dare nomi (Impostazioni, Funzioni).',
                    ),
                  Padding(
                    padding: const EdgeInsets.fromLTRB(16, 0, 16, 16),
                    child: CupertinoSlidingSegmentedControl<bool>(
                      groupValue: _labeling,
                      onValueChanged: (x) => setState(() => _labeling = x ?? _labeling),
                      children: {
                        false: const Text('Persone'),
                        true: Text('Da etichettare${todo > 0 ? ' · $todo' : ''}'),
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
    final index = _convs?.people ?? {};
    final shown = [
      for (final p in people.people)
        if (_type == null || (index[p.id]?.types.contains(_type) ?? false)) p,
    ];
    final label = conversationTypes
        .firstWhere((t) => t.$1 == _type, orElse: () => ('', 'Tutte', CupertinoIcons.circle))
        .$2;
    return [
      FilterPills<String>(values: conversationTypes, selected: _type, onChanged: _setType),
      const SizedBox(height: 16),
      if (_type == null) ...[_meCard(context, people.me), const SizedBox(height: 20)],
      if (_type != 'media')
        Section(
          header: _type == null ? 'Persone note · ${people.people.length}' : 'Persone · $label',
          children: shown.isEmpty
              ? [
                  IosRow(
                    title: _type == null ? 'Nessuna ancora' : 'Nessuna persona con un nome',
                    subtitle: 'Dai un nome alle voci in «Da etichettare».',
                  ),
                ]
              : [for (final p in shown) _personRow(context, p)],
        ),
      if (_convs != null)
        ConversationSection(
          api: widget.api,
          list: _convs!,
          previousPageTitle: 'Persone',
          header: _type == null ? 'Conversazioni recenti' : 'Conversazioni · $label',
          limit: 8,
          onMore: () => Navigator.of(context).push(
            CupertinoPageRoute(
              builder: (_) => ConversationsPage(
                api: widget.api,
                title: _type == null ? 'Conversazioni' : label,
                type: _type,
                previousPageTitle: 'Persone',
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
                    ? '${duration(me.minutes)} · ${me.sessions} sessioni'
                          '${me.lastSeen == null ? '' : ' · ultima ${shortDay(me.lastSeen!)}'}'
                    : 'Profilo vocale non ancora creato (sul Mac: omy voice enroll)',
                style: Ios.footnote(context),
              ),
            ],
          ),
        ),
      ],
    ),
  );

  Widget _personRow(BuildContext context, Person p) {
    final places = _convs?.people[p.id]?.places ?? [];
    return IosRow(
      leading: PersonAvatar(name: p.name),
      title: p.name,
      subtitle: [
        duration(p.minutes),
        '${p.sessions} ${p.sessions == 1 ? 'sessione' : 'sessioni'}',
        if (p.lastSeen != null) 'ultima ${shortDay(p.lastSeen!)}',
        if (places.isNotEmpty) places.first,
      ].join(' · '),
      trailing: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          for (final t in _convs?.people[p.id]?.types ?? <String>[])
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
      return [const Empty('Nessuna voce da etichettare negli ultimi giorni: tutte hanno un nome o sono video.')];
    }
    return [
      for (final s in sessions)
        Section(
          header: [if (s.day != null) longDay(s.day!), if (s.start != null) _localTime(s.start!)].join(' · '),
          headerTrailing: CupertinoButton(
            padding: EdgeInsets.zero,
            minimumSize: const Size(0, 28),
            onPressed: _busy ? null : () => _labelSession(s),
            child: const Text('Segna tutte…'),
          ),
          children: [
            for (final voice in s.voices)
              if (voice.todo) _voiceRow(context, s, voice),
          ],
        ),
      const Padding(
        padding: EdgeInsets.fromLTRB(32, 0, 32, 0),
        child: Empty('Le sessioni degli ultimi 14 giorni con voci senza nome né tipo video.'),
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
      title: v.pinned ? '${v.name} ✓' : v.name,
      titleStyle: Ios.headline(
        context,
      ).copyWith(color: known ? personColor(context, v.name, me: v.person == 'me') : null),
      subtitle: '${voiceTypeLabel(v)} · ${v.seconds < 60 ? '${v.seconds.round()} s' : duration(v.seconds / 60)}',
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
        child: Text(v.todo ? 'Etichetta' : 'Cambia'),
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

String voiceTypeLabel(Voice v) => switch (v.type) {
  'media' => v.app == null || v.app == 'Video' ? 'Video' : 'Video · ${v.app}',
  'meeting' =>
    v.app == 'iPhone'
        ? 'Chiamata'
        : v.app == null || v.app == 'Riunione'
        ? 'Riunione online'
        : 'Riunione · ${v.app}',
  _ => 'Di persona',
};

/// Where a voice comes from and, unless a TV, who it is: you first, then
/// the known people. Returns (type, name).
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

  void _done([String? name]) => Navigator.pop(context, (_type, (name ?? _c.text).trim()));

  @override
  Widget build(BuildContext context) {
    final v = widget.voice;
    final q = _c.text.trim().toLowerCase();
    final names = [?widget.me, ...widget.known].where((s) => q.isEmpty || s.toLowerCase().contains(q)).take(20);
    final named = _type != 'media' && widget.prints;
    return SheetPage(
      controller: widget.controller,
      title: 'Chi è?',
      action: 'Salva',
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
            children: const {
              'room': _Segment(CupertinoIcons.person_fill, 'Di persona'),
              'meeting': _Segment(CupertinoIcons.videocam_fill, 'Online'),
              'media': _Segment(CupertinoIcons.play_rectangle_fill, 'Video'),
            },
          ),
        ),
        Padding(
          padding: const EdgeInsets.fromLTRB(32, 4, 32, 12),
          child: Text(switch (_type) {
            'media' =>
              'Una voce da uno schermo o da un altoparlante: nessun nome, nessuna impronta vocale. '
                  'Nelle trascrizioni diventa «Video».',
            'meeting' =>
              v.kind == 'call' && v.app != null && v.app != 'Riunione'
                  ? 'Riconosciuta dal Mac: ${v.app}. Puoi dirle chi è.'
                  : 'Qualcuno in una chiamata o una riunione online (Teams, Zoom, Meet).',
            _ => 'Qualcuno nella stanza con te.',
          }, style: Ios.footnote(context)),
        ),
        if (named) ...[
          CupertinoFormSection.insetGrouped(
            children: [
              CupertinoTextFormFieldRow(
                controller: _c,
                placeholder: 'Nome (facoltativo)',
                textCapitalization: TextCapitalization.words,
                onChanged: (_) => setState(() {}),
                onFieldSubmitted: _done,
              ),
            ],
          ),
          if (names.isNotEmpty)
            Section(
              header: 'Scegli',
              children: [
                for (final n in names)
                  IosRow(
                    leading: PersonAvatar(name: n, me: n == widget.me, size: 30),
                    title: n,
                    titleStyle: Ios.body(context),
                    onTap: () => _done(n),
                  ),
              ],
            ),
        ] else if (_type != 'media')
          const Empty('Impronte vocali disattivate: si può scegliere il tipo, ma non dare un nome.'),
      ],
    );
  }
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
