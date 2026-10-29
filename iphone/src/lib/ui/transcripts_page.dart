import 'dart:async';

import 'package:flutter/cupertino.dart';

import '../api/mac_api.dart';
import '../api/models.dart';
import 'app_shell.dart';
import 'chat.dart';
import 'common.dart';
import 'conversations.dart';
import 'timeline.dart';
import '../l10n.dart';

/// "Conversazioni": every conversation in one timeline, filtered by a person
/// or a kind, and a search over all the transcripts.
class TranscriptsPage extends StatefulWidget {
  final MacApi api;
  final TabShown shown;
  const TranscriptsPage({super.key, required this.api, required this.shown});

  @override
  State<TranscriptsPage> createState() => _TranscriptsPageState();
}

class _TranscriptsPageState extends State<TranscriptsPage> {
  late ConversationFeed _feed = ConversationFeed(widget.api);
  Pending? _pending;
  final _query = TextEditingController();
  Timer? _debounce;
  SearchResult? _results;
  String? _searched;
  bool _searching = false;
  ({String id, String name})? _person;
  String? _type;

  @override
  void initState() {
    super.initState();
    widget.shown.addListener(_load);
    _load();
  }

  @override
  void dispose() {
    widget.shown.removeListener(_load);
    _feed.dispose();
    _debounce?.cancel();
    _query.dispose();
    super.dispose();
  }

  Future<void> _load() async {
    await _feed.refresh();
    try {
      final d = await widget.api.days();
      if (mounted) setState(() => _pending = d.pending);
    } on MacError {
      // The feed shows the error.
    }
  }

  void _setFilter({({String id, String name})? person, String? type}) {
    _feed.dispose();
    setState(() {
      _person = person;
      _type = type;
      _feed = ConversationFeed(widget.api, person: person?.id, type: type);
    });
    _load();
    final q = _query.text.trim();
    if (q.length >= 2) _search(q);
  }

  /// The people as they are now: named since, renamed, merged.
  Future<void> _choosePerson() async {
    final People p;
    try {
      p = await widget.api.people();
    } on MacError catch (e) {
      if (mounted) showMessage(context, e, title: tr('Non riuscito', 'Failed'));
      return;
    }
    if (!mounted) return;
    final id = await chooseAction(
      context,
      title: tr('Conversazioni con', 'Conversations with'),
      actions: [
        if (_person != null) ('', tr('Tutti', 'Everyone'), false),
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
      if (mounted) showMessage(context, e, title: tr('Ricerca non riuscita', 'Search failed'));
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
          _feed.list?.conversations.where((c) => c.session == r.session).firstOrNull ?? Conversation.of(r),
    ];
    ConversationViewer.open(
      context,
      widget.api,
      convs,
      convs.indexWhere((c) => c.session == p.session),
      previousPageTitle: tr('Risultati', 'Results'),
      focus: p.id,
      highlight: _searched,
    );
  }

  @override
  Widget build(BuildContext context) {
    final p = _pending;
    return ListenableBuilder(
      listenable: _feed,
      builder: (context, _) => Timeline(
        api: widget.api,
        title: tr('Conversazioni', 'Conversations'),
        scrollToTop: widget.shown.retap,
        onRefresh: _load,
        busy: _searching,
        conversations: _feed.list?.conversations,
        hasMore: _feed.hasMore,
        onMore: _feed.more,
        error: _feed.error,
        onRetry: _load,
        empty: _person != null || _type != null
            ? tr('Nessuna conversazione con questi filtri.', 'No conversations with these filters.')
            : tr('Ancora nessuna trascrizione.', 'No transcripts yet.'),
        searchField: CupertinoSearchTextField(
          controller: _query,
          placeholder: tr('Cerca', 'Search'),
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
                    ? tr(
                        'Il Mac sta trascrivendo (${p.progress.sessionsDone} di ${p.progress.sessionsTotal} sessioni).',
                        'The Mac is transcribing (${p.progress.sessionsDone} of ${p.progress.sessionsTotal} sessions).',
                      )
                    : tr(
                        '${counted(p.sessions, 'sessione', 'sessioni', '', '')} da trascrivere (${duration(p.minutes)}): '
                            'da Oggi puoi farle partire subito.',
                        '${counted(p.sessions, '', '', 'session', 'sessions')} to transcribe (${duration(p.minutes)}): '
                            'you can start them now from Today.',
                      ),
              ),
            ),
        ],
        replacement: _results == null ? null : _resultSlivers(context),
      ),
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
                    _person?.name ?? tr('Persona', 'Person'),
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
              all: tr('Tutte', 'All'),
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
              Text(
                tr('Nessun risultato per «$_searched»', 'No results for “$_searched”'),
                style: Ios.headline(context),
              ),
            ],
          ),
        ),
      ];
    }
    return [
      SliverToBoxAdapter(
        child: Section(
          header: '${counted(r.results.length, 'risultato', 'risultati', 'result', 'results')}${r.more ? '+' : ''}',
          children: [
            for (final p in r.results)
              IosRow(
                leading: PersonAvatar(name: p.speaker, me: p.isMe, size: 34),
                title: p.speaker ?? tr('Voce sconosciuta', 'Unknown voice'),
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
