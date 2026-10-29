import 'dart:async';

import 'package:flutter/cupertino.dart';

import '../api/mac_api.dart';
import '../api/models.dart';
import '../features.dart';
import 'app_shell.dart';
import 'common.dart';

/// "Persone": you, the people you named, and the voices of a day to name.
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
  List<String> _days = [];
  String? _day;
  DayVoices? _voices;
  Object? _error;
  bool _busy = false;
  String? _playing;
  Timer? _playTimer;
  bool _todoOnly = true;

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
      _days = (await widget.api.days()).days.map((d) => d.day).toList();
      if (_day == null || !_days.contains(_day)) _day = _days.firstOrNull;
      final voices = _day == null ? null : await widget.api.voices(_day!);
      if (!mounted) return;
      setState(() {
        _people = people;
        _voices = voices;
      });
    } on MacError catch (e) {
      if (mounted) setState(() => _error = e);
    }
  }

  Future<void> _selectDay(String day) async {
    setState(() {
      _day = day;
      _voices = null;
    });
    try {
      final v = await widget.api.voices(day);
      if (mounted && _day == day) setState(() => _voices = v);
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

  /// The voices without a name of a session, all at once: a TV on, a meeting.
  Future<void> _labelSession(VoiceSession s) async {
    final unnamed = [
      for (final v in s.voices)
        if (v.person == null) v.label,
    ];
    if (unnamed.isEmpty) return;
    final type = await chooseAction(
      context,
      title: unnamed.length == 1 ? 'La voce senza nome' : 'Le ${unnamed.length} voci senza nome',
      message: 'Di questa sessione, tutte insieme.',
      actions: [
        ('media', 'Sono TV o video', false),
        ('meeting', 'Sono in una riunione online', false),
        ('room', 'Sono di persona', false),
        ('auto', 'Come le ha riconosciute il Mac', false),
      ],
    );
    if (type != null) await _change(() => widget.api.setVoiceKind(s.session, unnamed, type));
  }

  Future<void> _personActions(Person p) async {
    final a = await chooseAction(
      context,
      title: p.name,
      actions: [('rename', 'Rinomina', false), ('delete', 'Elimina', true)],
    );
    if (!mounted) return;
    if (a == 'rename') await _renamePerson(p);
    if (a == 'delete') await _deletePerson(p);
  }

  Future<void> _renamePerson(Person p) async {
    final name = await askName(context, title: 'Rinomina', initial: p.name);
    if (name == null || name.isEmpty || name == p.name || !mounted) return;
    final merging = _people!.people.any((o) => o.id != p.id && o.name.toLowerCase() == name.toLowerCase());
    if (merging &&
        !await confirm(
          context,
          title: 'Unire a $name?',
          message: 'Esiste già $name: le due persone diventano una sola, con le voci di entrambe.',
          action: 'Unisci',
          destructive: false,
        )) {
      return;
    }
    await _change(() => widget.api.renamePerson(p.id, name));
  }

  Future<void> _deletePerson(Person p) async {
    if (!await confirm(
      context,
      title: 'Eliminare ${p.name}?',
      message:
          'Si cancellano il nome e l\'impronta vocale. Le sue voci tornano senza nome e si possono nominare di nuovo.',
    )) {
      return;
    }
    await _change(() => widget.api.deletePerson(p.id));
  }

  @override
  Widget build(BuildContext context) {
    final people = _people;
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
                  _meCard(context, people.me),
                  const SizedBox(height: 20),
                  if (people.people.isEmpty)
                    const Section(
                      header: 'Persone note',
                      children: [IosRow(title: 'Nessuna ancora', subtitle: 'Dai un nome a una voce qui sotto.')],
                    )
                  else
                    Section(
                      header: 'Persone note · ${people.people.length}',
                      children: [for (final p in people.people) _personRow(context, p)],
                    ),
                  Padding(
                    padding: const EdgeInsets.fromLTRB(32, 16, 32, 8),
                    child: Text('VOCI DEL GIORNO', style: Ios.footnote(context)),
                  ),
                  _dayPicker(context),
                  const SizedBox(height: 12),
                  ..._voiceSections(context),
                ],
              ),
            ],
    );
  }

  Widget _meCard(BuildContext context, Me me) => InsetCard(
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

  Widget _personRow(BuildContext context, Person p) => IosRow(
    leading: PersonAvatar(name: p.name),
    title: p.name,
    subtitle:
        '${duration(p.minutes)} · ${p.sessions} ${p.sessions == 1 ? 'sessione' : 'sessioni'}'
        '${p.lastSeen == null ? '' : ' · ultima ${shortDay(p.lastSeen!)}'}',
    trailing: Icon(CupertinoIcons.ellipsis_circle, color: resolve(context, tint)),
    onTap: _busy ? null : () => _personActions(p),
  );

  Widget _dayPicker(BuildContext context) => SizedBox(
    height: 34,
    child: ListView(
      scrollDirection: Axis.horizontal,
      padding: const EdgeInsets.symmetric(horizontal: 16),
      children: [
        for (final d in _days.take(14))
          Padding(
            padding: const EdgeInsets.only(right: 8),
            child: CupertinoButton(
              padding: const EdgeInsets.symmetric(horizontal: 14),
              minimumSize: const Size(0, 34),
              borderRadius: BorderRadius.circular(17),
              color: d == _day
                  ? resolve(context, tint)
                  : resolve(context, CupertinoColors.secondarySystemGroupedBackground),
              onPressed: () => _selectDay(d),
              child: Text(
                longDay(d) == 'Oggi' || longDay(d) == 'Ieri' ? longDay(d) : shortDay(d),
                style: Ios.subhead(context).copyWith(
                  color: d == _day ? const Color(0xFFFFFFFF) : null,
                  fontWeight: d == _day ? FontWeight.w600 : null,
                ),
              ),
            ),
          ),
      ],
    ),
  );

  List<Widget> _voiceSections(BuildContext context) {
    final v = _voices;
    if (_day == null) return [const Empty('Ancora nessuna trascrizione.')];
    if (v == null) return [const Padding(padding: EdgeInsets.all(24), child: CupertinoActivityIndicator())];
    if (v.sessions.isEmpty) return [const Empty('Nessuna voce separata in questo giorno.')];
    final todo = v.sessions.fold(0, (n, s) => n + s.voices.where((x) => x.todo).length);
    final shown = [
      for (final s in v.sessions)
        if (!_todoOnly || s.voices.any((x) => x.todo)) s,
    ];
    return [
      Padding(
        padding: const EdgeInsets.fromLTRB(16, 0, 16, 16),
        child: CupertinoSlidingSegmentedControl<bool>(
          groupValue: _todoOnly,
          onValueChanged: (x) => setState(() => _todoOnly = x ?? _todoOnly),
          children: {true: Text('Da etichettare · $todo'), false: const Text('Tutte')},
        ),
      ),
      if (shown.isEmpty) const Empty('Tutte le voci di questo giorno hanno un nome o un tipo.'),
      for (final s in shown)
        Section(
          header: s.start == null ? 'Sessione' : 'Sessione delle ${_localTime(s.start!)}',
          headerTrailing: s.voices.any((x) => x.person == null)
              ? CupertinoButton(
                  padding: EdgeInsets.zero,
                  minimumSize: const Size(0, 28),
                  onPressed: _busy ? null : () => _labelSession(s),
                  child: const Text('Tutte…'),
                )
              : null,
          children: [
            for (final voice in s.voices)
              if (!_todoOnly || voice.todo) _voiceRow(context, s, voice),
          ],
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
  'media' => CupertinoIcons.tv_fill,
  'meeting' => v.app == 'iPhone' ? CupertinoIcons.phone_fill : CupertinoIcons.videocam_fill,
  _ => CupertinoIcons.person_fill,
};

String voiceTypeLabel(Voice v) => switch (v.type) {
  'media' => v.app == null || v.app == 'TV' ? 'TV o video' : 'Media · ${v.app}',
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
              'media': _Segment(CupertinoIcons.tv_fill, 'TV / video'),
            },
          ),
        ),
        Padding(
          padding: const EdgeInsets.fromLTRB(32, 4, 32, 12),
          child: Text(switch (_type) {
            'media' =>
              'Una voce da uno schermo o da un altoparlante: nessun nome, nessuna impronta vocale. '
                  'Nelle trascrizioni diventa «TV».',
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
