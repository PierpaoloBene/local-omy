import 'dart:async';

import 'package:flutter/cupertino.dart';

import '../api/mac_api.dart';
import '../api/models.dart';
import 'app_shell.dart';
import 'chat.dart';
import 'common.dart';
import 'conversations.dart';
import 'timeline.dart';

/// "Conversazioni": every conversation in one timeline, filtered by a person
/// or a kind, and a search over all the transcripts.
class TranscriptsPage extends StatefulWidget {
  final MacApi api;
  final TabShown shown;
  const TranscriptsPage({super.key, required this.api, required this.shown});

  @override
  State<TranscriptsPage> createState() => _TranscriptsPageState();
}

/// All of them: the timeline jumps to any day.
const timelineLimit = 5000;

class _TranscriptsPageState extends State<TranscriptsPage> {
  ConversationList? _list;
  Pending? _pending;
  Object? _error;
  final _query = TextEditingController();
  Timer? _debounce;
  SearchResult? _results;
  String? _searched;
  bool _searching = false;
  ({String id, String name})? _person;
  String? _type;
  People? _people;

  @override
  void initState() {
    super.initState();
    widget.shown.addListener(_load);
    _load();
  }

  @override
  void dispose() {
    widget.shown.removeListener(_load);
    _debounce?.cancel();
    _query.dispose();
    super.dispose();
  }

  Future<void> _load() async {
    setState(() => _error = null);
    final person = _person?.id, type = _type;
    try {
      final l = await widget.api.conversations(person: person, type: type, limit: timelineLimit);
      if (mounted && _person?.id == person && _type == type) setState(() => _list = l);
      final d = await widget.api.days();
      if (mounted) setState(() => _pending = d.pending);
    } on MacError catch (e) {
      if (mounted) setState(() => _error = e);
    }
  }

  void _setFilter({({String id, String name})? person, String? type}) {
    setState(() {
      _person = person;
      _type = type;
      _list = null;
    });
    _load();
    final q = _query.text.trim();
    if (q.length >= 2) _search(q);
  }

  Future<void> _choosePerson() async {
    try {
      _people ??= await widget.api.people();
    } on MacError catch (e) {
      if (mounted) showMessage(context, e, title: 'Non riuscito');
      return;
    }
    if (!mounted) return;
    final p = _people!;
    final id = await chooseAction(
      context,
      title: 'Conversazioni con',
      actions: [
        if (_person != null) ('', 'Tutti', false),
        ('me', p.me.name, false),
        for (final x in p.people) (x.id, x.name, false),
      ],
    );
    if (id == null) return;
    _setFilter(
      person: id.isEmpty ? null : (id: id, name: id == 'me' ? p.me.name : p.people.firstWhere((x) => x.id == id).name),
      type: _type,
    );
  }

  void _onQuery(String q) {
    _debounce?.cancel();
    if (q.trim().length < 2) {
      setState(() {
        _results = null;
        _searched = null;
      });
      return;
    }
    _debounce = Timer(const Duration(milliseconds: 400), () => _search(q.trim()));
  }

  Future<void> _search(String q) async {
    setState(() => _searching = true);
    try {
      final person = _person?.id, type = _type;
      final r = await widget.api.search(q, person: person, type: type);
      if (mounted && _query.text.trim() == q && _person?.id == person && _type == type) {
        setState(() {
          _results = r;
          _searched = q;
        });
      }
    } on MacError catch (e) {
      if (mounted) showMessage(context, e, title: 'Ricerca non riuscita');
    } finally {
      if (mounted) setState(() => _searching = false);
    }
  }

  /// A search result: its conversation, among those of the other results.
  void _openResult(Paragraph p) {
    final sessions = <String>{};
    final convs = [
      for (final r in _results!.results)
        if (sessions.add(r.session))
          _list?.conversations.where((c) => c.session == r.session).firstOrNull ?? Conversation.of(r),
    ];
    ConversationViewer.open(
      context,
      widget.api,
      convs,
      convs.indexWhere((c) => c.session == p.session),
      previousPageTitle: 'Risultati',
      focus: p.id,
      highlight: _searched,
    );
  }

  @override
  Widget build(BuildContext context) {
    final p = _pending;
    return Timeline(
      api: widget.api,
      title: 'Conversazioni',
      onRefresh: _load,
      busy: _searching,
      conversations: _list?.conversations,
      error: _error,
      onRetry: _load,
      empty: _person != null || _type != null
          ? 'Nessuna conversazione con questi filtri.'
          : 'Ancora nessuna trascrizione.',
      searchField: CupertinoSearchTextField(
        controller: _query,
        placeholder: 'Cerca nelle trascrizioni',
        onChanged: _onQuery,
        onSubmitted: (q) => q.trim().length >= 2 ? _search(q.trim()) : null,
        onSuffixTap: () {
          _query.clear();
          _onQuery('');
        },
      ),
      top: [
        SliverToBoxAdapter(child: _filters(context)),
        if (_results == null && p != null && (p.progress.running || p.sessions > 0))
          SliverToBoxAdapter(
            child: Notice(
              icon: p.progress.running ? CupertinoIcons.hourglass : CupertinoIcons.clock,
              text: p.progress.running
                  ? 'Il Mac sta trascrivendo (${p.progress.sessionsDone} di ${p.progress.sessionsTotal} sessioni).'
                  : '${p.sessions} ${p.sessions == 1 ? 'sessione' : 'sessioni'} da trascrivere (${duration(p.minutes)}): '
                        'da Oggi puoi farle partire subito.',
            ),
          ),
      ],
      replacement: _results == null ? null : _resultSlivers(context),
    );
  }

  Widget _filters(BuildContext context) {
    final on = _person != null;
    final fg = on ? const Color(0xFFFFFFFF) : resolve(context, CupertinoColors.label);
    return Padding(
      padding: const EdgeInsets.only(bottom: 12),
      child: Row(
        children: [
          Padding(
            padding: const EdgeInsets.only(left: 16),
            child: CupertinoButton(
              padding: const EdgeInsets.symmetric(horizontal: 13),
              minimumSize: const Size(0, 32),
              borderRadius: BorderRadius.circular(16),
              color: on ? resolve(context, tint) : resolve(context, CupertinoColors.secondarySystemGroupedBackground),
              onPressed: _choosePerson,
              child: Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Icon(CupertinoIcons.person_fill, size: 15, color: fg),
                  const SizedBox(width: 5),
                  Text(
                    _person?.name ?? 'Persona',
                    style: Ios.subhead(context).copyWith(color: fg, fontWeight: on ? FontWeight.w600 : null),
                  ),
                  const SizedBox(width: 3),
                  Icon(CupertinoIcons.chevron_down, size: 12, color: fg),
                ],
              ),
            ),
          ),
          Expanded(
            child: FilterPills<String>(
              values: conversationTypes,
              selected: _type,
              all: 'Tutte',
              onChanged: (t) => _setFilter(person: _person, type: t),
            ),
          ),
        ],
      ),
    );
  }

  List<Widget> _resultSlivers(BuildContext context) {
    final r = _results!;
    if (r.results.isEmpty) {
      return [
        SliverFillRemaining(
          hasScrollBody: false,
          child: Column(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              Icon(CupertinoIcons.search, size: 44, color: resolve(context, CupertinoColors.tertiaryLabel)),
              const SizedBox(height: 10),
              Text('Nessun risultato per «$_searched»', style: Ios.headline(context)),
            ],
          ),
        ),
      ];
    }
    return [
      SliverToBoxAdapter(
        child: Section(
          header: '${r.results.length}${r.more ? '+' : ''} ${r.results.length == 1 ? 'risultato' : 'risultati'}',
          children: [
            for (final p in r.results)
              IosRow(
                leading: PersonAvatar(name: p.speaker, me: p.isMe, size: 34),
                title: p.speaker ?? 'Voce sconosciuta',
                titleStyle: Ios.headline(context).copyWith(fontSize: 15),
                value: '${shortDay(p.day!)} ${hm(p.start)}',
                chevron: true,
                below: Padding(
                  padding: const EdgeInsets.only(top: 2),
                  child: Text.rich(
                    highlighted(context, p.text, _searched),
                    maxLines: 3,
                    overflow: TextOverflow.ellipsis,
                    style: Ios.subhead(context).copyWith(color: resolve(context, CupertinoColors.secondaryLabel)),
                  ),
                ),
                onTap: () => _openResult(p),
              ),
          ],
        ),
      ),
    ];
  }
}
