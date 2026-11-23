import 'dart:async';

import 'package:flutter/cupertino.dart';

import '../api/mac_api.dart';
import '../api/models.dart';
import '../l10n.dart';
import 'app_shell.dart';
import 'chat.dart';
import 'common.dart';

/// One message of the chat, kept in memory only.
class _Msg {
  final bool mine;
  String text;
  List<Citation> citations = [];
  String? stage, error;
  bool done = false, notFound = false;
  double? totalMs, firstMs;

  /// For the retry of a failed answer.
  final String? question;
  _Msg.user(this.text) : mine = true, question = null, done = true;
  _Msg.answer(this.question) : mine = false, text = '';
  bool get waiting => !mine && !done && error == null;
}

/// The Chiedi tab: ask the archive, the answer streams in with its sources.
class AskPage extends StatefulWidget {
  final MacApi api;
  final TabShown? shown;
  const AskPage({super.key, required this.api, this.shown});

  @override
  State<AskPage> createState() => _AskPageState();
}

class _AskPageState extends State<AskPage> {
  final _msgs = <_Msg>[];
  final _input = TextEditingController();
  final _scroll = ScrollController();
  StreamSubscription<AskEvent>? _sub;
  bool _busy = false;

  @override
  void dispose() {
    _sub?.cancel();
    _input.dispose();
    _scroll.dispose();
    super.dispose();
  }

  void _toEnd() => WidgetsBinding.instance.addPostFrameCallback((_) {
    if (_scroll.hasClients) _scroll.jumpTo(_scroll.position.maxScrollExtent);
  });

  List<AskTurn> _history(int before) => [
    for (final m in _msgs.take(before))
      if (m.mine || (m.done && !m.notFound && m.text.isNotEmpty)) AskTurn(m.mine ? 'user' : 'assistant', m.text),
  ];

  void _send() {
    final q = _input.text.trim();
    if (q.isEmpty || _busy) return;
    _input.clear();
    _msgs.add(_Msg.user(q));
    _answer(q, _Msg.answer(q));
  }

  void _retry(_Msg failed) {
    if (_busy) return;
    final i = _msgs.indexOf(failed);
    _msgs.removeAt(i);
    _answer(failed.question!, _Msg.answer(failed.question), at: i);
  }

  /// [at]: where the answer goes (the history is what is before the question).
  void _answer(String q, _Msg a, {int? at}) {
    final history = _history((at ?? _msgs.length) - 1);
    at == null ? _msgs.add(a) : _msgs.insert(at, a);
    final clock = Stopwatch()..start();
    setState(() => _busy = true);
    _toEnd();
    void finish() {
      _busy = false;
      a.stage = null;
      _sub?.cancel();
    }

    _sub = widget.api
        .ask(q, history)
        .listen(
          (e) {
            if (!mounted) return;
            setState(() {
              switch (e) {
                case AskStatus():
                  a.stage = e.stage;
                case AskCitations():
                  a.citations = e.items;
                case AskDelta():
                  if (e.text.isNotEmpty) {
                    a.firstMs ??= clock.elapsedMicroseconds / 1000;
                    a.stage = null;
                    a.text += e.text;
                  }
                case AskDone():
                  a.done = true;
                  a.notFound = e.notFound;
                  a.totalMs = clock.elapsedMicroseconds / 1000;
                  finish();
                case AskError():
                  a.error = e.message.isEmpty ? tr('Errore dal Mac', 'Error from the Mac') : e.message;
                  finish();
              }
            });
            _toEnd();
          },
          onError: (Object e) {
            if (!mounted) return;
            setState(() {
              a.error = '$e';
              finish();
            });
          },
          onDone: () {
            // The stream ended with no done: don't wait forever.
            if (!mounted || !a.waiting) return;
            setState(() {
              a.error = tr('Risposta interrotta', 'The answer was cut short');
              finish();
            });
          },
          cancelOnError: true,
        );
  }

  void _clear() {
    _sub?.cancel();
    setState(() {
      _msgs.clear();
      _busy = false;
    });
  }

  /// The conversation of a source, open at the paragraph.
  Future<void> _open(Citation c) async {
    try {
      final list = await widget.api.conversations(day: c.day);
      final i = list.conversations.indexWhere((x) => x.session == c.session);
      if (!mounted) return;
      if (i < 0) {
        await showMessage(context, tr('Conversazione non trovata', 'Conversation not found'));
        return;
      }
      await ConversationViewer.open(
        context,
        widget.api,
        list.conversations,
        i,
        previousPageTitle: tr('Chiedi', 'Ask'),
        focus: c.rowId.isEmpty ? null : c.rowId,
      );
    } on MacError catch (e) {
      if (mounted) showMessage(context, e);
    }
  }

  void _note(Citation c) => showCupertinoModalPopup<void>(
    context: context,
    builder: (_) => _NoteSheet(api: widget.api, citation: c),
  );

  @override
  Widget build(BuildContext context) {
    return CupertinoPageScaffold(
      backgroundColor: CupertinoColors.systemGroupedBackground,
      navigationBar: CupertinoNavigationBar(
        middle: Text(tr('Chiedi', 'Ask')),
        trailing: _msgs.isEmpty
            ? null
            : CupertinoButton(
                padding: EdgeInsets.zero,
                onPressed: _clear,
                child: Text(tr('Nuova chat', 'New chat')),
              ),
      ),
      child: SafeArea(
        child: Column(
          children: [
            Expanded(
              child: _msgs.isEmpty
                  ? Center(
                      child: Empty(
                        tr(
                          'Chiedi qualcosa sulle tue conversazioni: "Cosa ho promesso a Marco la settimana scorsa?"',
                          'Ask about your conversations: "What did I promise Marco last week?"',
                        ),
                      ),
                    )
                  : ListView.builder(
                      controller: _scroll,
                      keyboardDismissBehavior: ScrollViewKeyboardDismissBehavior.onDrag,
                      padding: const EdgeInsets.symmetric(vertical: 12),
                      itemCount: _msgs.length,
                      itemBuilder: (context, i) => _msgs[i].mine
                          ? _UserBubble(_msgs[i].text)
                          : _AnswerView(
                              msg: _msgs[i],
                              onRetry: () => _retry(_msgs[i]),
                              onOpen: _open,
                              onNote: _note,
                            ),
                    ),
            ),
            _InputBar(controller: _input, enabled: !_busy, onSend: _send),
          ],
        ),
      ),
    );
  }
}

class _InputBar extends StatelessWidget {
  final TextEditingController controller;
  final bool enabled;
  final VoidCallback onSend;
  const _InputBar({required this.controller, required this.enabled, required this.onSend});

  @override
  Widget build(BuildContext context) => Container(
    padding: const EdgeInsets.fromLTRB(12, 8, 6, 8),
    color: resolve(context, CupertinoColors.systemBackground),
    child: Row(
      crossAxisAlignment: CrossAxisAlignment.end,
      children: [
        Expanded(
          child: CupertinoTextField(
            key: const Key('ask-input'),
            controller: controller,
            enabled: enabled,
            minLines: 1,
            maxLines: 5,
            textCapitalization: TextCapitalization.sentences,
            textInputAction: TextInputAction.send,
            onSubmitted: (_) => onSend(),
            placeholder: tr('Chiedi all\'archivio…', 'Ask the archive…'),
            padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 9),
            decoration: BoxDecoration(
              color: resolve(context, CupertinoColors.secondarySystemFill),
              borderRadius: BorderRadius.circular(18),
            ),
          ),
        ),
        ValueListenableBuilder<TextEditingValue>(
          valueListenable: controller,
          builder: (context, v, _) => CupertinoButton(
            key: const Key('ask-send'),
            padding: const EdgeInsets.symmetric(horizontal: 6),
            minimumSize: const Size(36, 36),
            onPressed: enabled && v.text.trim().isNotEmpty ? onSend : null,
            child: const Icon(CupertinoIcons.arrow_up_circle_fill, size: 32),
          ),
        ),
      ],
    ),
  );
}

class _UserBubble extends StatelessWidget {
  final String text;
  const _UserBubble(this.text);

  @override
  Widget build(BuildContext context) => Align(
    alignment: Alignment.centerRight,
    child: Container(
      margin: const EdgeInsets.fromLTRB(60, 3, 12, 3),
      padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 9),
      decoration: BoxDecoration(color: resolve(context, tint), borderRadius: BorderRadius.circular(18)),
      child: Text(text, style: Ios.body(context).copyWith(color: CupertinoColors.white)),
    ),
  );
}

class _AnswerView extends StatelessWidget {
  final _Msg msg;
  final VoidCallback onRetry;
  final void Function(Citation) onOpen, onNote;
  const _AnswerView({required this.msg, required this.onRetry, required this.onOpen, required this.onNote});

  String? get _status => switch (msg.stage) {
    'waking' => tr('Sveglio il modello…', 'Waking the model…'),
    'searching' => tr('Cerco nell\'archivio…', 'Searching the archive…'),
    'answering' => tr('Scrivo la risposta…', 'Writing the answer…'),
    _ => msg.waiting && msg.text.isEmpty ? tr('Cerco nell\'archivio…', 'Searching the archive…') : null,
  };

  static String _seconds(double ms) => (ms / 1000).toStringAsFixed(1).replaceAll('.', english ? '.' : ',');

  String? get _timing {
    final t = msg.totalMs;
    if (t == null) return null;
    final f = msg.firstMs;
    return f == null
        ? '${_seconds(t)} s'
        : '${_seconds(t)} s · ${tr('primo testo', 'first text')} ${_seconds(f)} s';
  }

  @override
  Widget build(BuildContext context) {
    final grey = resolve(context, CupertinoColors.secondaryLabel);
    final timing = _timing, status = _status;
    return Align(
      alignment: Alignment.centerLeft,
      child: Container(
        margin: const EdgeInsets.fromLTRB(12, 3, 40, 3),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            if (msg.text.isNotEmpty || msg.notFound)
              Container(
                key: msg.notFound ? const Key('ask-not-found') : null,
                padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 9),
                decoration: BoxDecoration(
                  color: msg.notFound
                      ? resolve(context, CupertinoColors.systemOrange).withValues(alpha: 0.12)
                      : resolve(context, CupertinoColors.secondarySystemGroupedBackground),
                  borderRadius: BorderRadius.circular(18),
                ),
                child: msg.notFound
                    ? Row(
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          Icon(CupertinoIcons.search, size: 16, color: grey),
                          const SizedBox(width: 8),
                          Flexible(
                            child: Text(msg.text, style: Ios.subhead(context).copyWith(color: grey)),
                          ),
                        ],
                      )
                    : _AnswerText(msg: msg, onOpen: onOpen, citations: msg.citations),
              ),
            if (status != null)
              Padding(
                padding: const EdgeInsets.fromLTRB(14, 6, 0, 2),
                child: Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    const CupertinoActivityIndicator(radius: 7),
                    const SizedBox(width: 8),
                    Text(status, key: const Key('ask-status'), style: Ios.caption(context)),
                  ],
                ),
              ),
            if (msg.error != null)
              Container(
                padding: const EdgeInsets.fromLTRB(14, 9, 6, 9),
                decoration: BoxDecoration(
                  color: resolve(context, CupertinoColors.systemRed).withValues(alpha: 0.12),
                  borderRadius: BorderRadius.circular(18),
                ),
                child: Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Flexible(child: Text(msg.error!, style: Ios.subhead(context))),
                    CupertinoButton(
                      key: const Key('ask-retry'),
                      padding: const EdgeInsets.symmetric(horizontal: 10),
                      minimumSize: const Size(30, 30),
                      onPressed: onRetry,
                      child: Text(tr('Riprova', 'Retry')),
                    ),
                  ],
                ),
              ),
            if (msg.citations.isNotEmpty && (msg.text.isNotEmpty || msg.done))
              Padding(
                padding: const EdgeInsets.only(top: 6),
                child: Wrap(
                  spacing: 6,
                  runSpacing: 6,
                  children: [for (final c in msg.citations) _Chip(c, onOpen: onOpen, onNote: onNote)],
                ),
              ),
            if (timing != null)
              Padding(
                padding: const EdgeInsets.fromLTRB(14, 4, 0, 0),
                child: Text(timing, key: const Key('ask-timing'), style: Ios.caption(context)),
              ),
          ],
        ),
      ),
    );
  }
}

/// The answer with its [n] as small tappable numbers raised on the line,
/// and while it streams a soft cursor.
class _AnswerText extends StatelessWidget {
  final _Msg msg;
  final List<Citation> citations;
  final void Function(Citation) onOpen;
  const _AnswerText({required this.msg, required this.citations, required this.onOpen});

  @override
  Widget build(BuildContext context) {
    final style = Ios.body(context);
    final spans = <InlineSpan>[];
    var last = 0;
    for (final m in RegExp(r'\[(\d+)\]').allMatches(msg.text)) {
      final n = int.parse(m.group(1)!);
      final c = citations.where((c) => c.n == n).firstOrNull;
      if (m.start > last) spans.add(TextSpan(text: msg.text.substring(last, m.start)));
      last = m.end;
      final label = Text('$n', style: style.copyWith(fontSize: 11, color: resolve(context, tint)));
      spans.add(
        c == null
            ? TextSpan(text: m.group(0), style: style.copyWith(fontSize: 11))
            : WidgetSpan(
                alignment: PlaceholderAlignment.baseline,
                baseline: TextBaseline.alphabetic,
                child: GestureDetector(
                  behavior: HitTestBehavior.opaque,
                  onTap: () => onOpen(c),
                  child: Padding(
                    padding: const EdgeInsets.symmetric(horizontal: 1.5, vertical: 4),
                    child: Transform.translate(offset: const Offset(0, -5), child: label),
                  ),
                ),
              ),
      );
    }
    if (last < msg.text.length) spans.add(TextSpan(text: msg.text.substring(last)));
    if (!msg.done && msg.error == null) {
      spans.add(TextSpan(text: ' ▍', style: TextStyle(color: resolve(context, tint).withValues(alpha: 0.45))));
    }
    return Text.rich(TextSpan(style: style, children: spans), key: const Key('ask-answer'));
  }
}

class _Chip extends StatelessWidget {
  final Citation c;
  final void Function(Citation) onOpen, onNote;
  const _Chip(this.c, {required this.onOpen, required this.onNote});

  @override
  Widget build(BuildContext context) {
    final color = resolve(context, tint);
    return GestureDetector(
      onTap: () => onOpen(c),
      onLongPress: () => onNote(c),
      child: Container(
        constraints: const BoxConstraints(maxWidth: 260),
        padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 5),
        decoration: BoxDecoration(color: color.withValues(alpha: 0.12), borderRadius: BorderRadius.circular(14)),
        child: Text(
          '[${c.n}] ${shortDay(c.day)}${c.time.isEmpty ? '' : ' ${c.time}'}${c.title.isEmpty ? '' : ' · ${c.title}'}',
          maxLines: 1,
          overflow: TextOverflow.ellipsis,
          style: Ios.caption(context).copyWith(color: color),
        ),
      ),
    );
  }
}

/// The note of a source: title, summary, key points, commitments, dates.
class _NoteSheet extends StatefulWidget {
  final MacApi api;
  final Citation citation;
  const _NoteSheet({required this.api, required this.citation});

  @override
  State<_NoteSheet> createState() => _NoteSheetState();
}

class _NoteSheetState extends State<_NoteSheet> {
  late final Future<Note?> _note = widget.api.note(widget.citation.session);

  Widget _block(BuildContext context, String title, List<String> items) => items.isEmpty
      ? const SizedBox()
      : Padding(
          padding: const EdgeInsets.only(top: 16),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(title.toUpperCase(), style: Ios.caption(context)),
              const SizedBox(height: 4),
              for (final s in items)
                Padding(
                  padding: const EdgeInsets.only(bottom: 4),
                  child: Text('• $s', style: Ios.subhead(context)),
                ),
            ],
          ),
        );

  @override
  Widget build(BuildContext context) {
    final h = MediaQuery.sizeOf(context).height * 0.8;
    return Container(
      height: h,
      decoration: BoxDecoration(
        color: resolve(context, CupertinoColors.systemGroupedBackground),
        borderRadius: const BorderRadius.vertical(top: Radius.circular(14)),
      ),
      child: DefaultTextStyle(
        style: Ios.body(context),
        child: Column(
          children: [
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 4, 4, 0),
              child: Row(
                children: [
                  Expanded(child: Text(tr('Nota', 'Note'), style: Ios.headline(context))),
                  CupertinoButton(
                    padding: const EdgeInsets.symmetric(horizontal: 12),
                    onPressed: () => Navigator.pop(context),
                    child: Text(tr('Chiudi', 'Close')),
                  ),
                ],
              ),
            ),
            Expanded(
              child: FutureBuilder<Note?>(
                future: _note,
                builder: (context, snap) {
                  if (snap.connectionState != ConnectionState.done) {
                    return const Center(child: CupertinoActivityIndicator());
                  }
                  if (snap.hasError) {
                    return Center(child: Text('${snap.error}', style: Ios.subhead(context)));
                  }
                  final n = snap.data;
                  if (n == null) {
                    return Center(
                      child: Text(
                        tr('Nessuna nota per questa conversazione', 'No note for this conversation'),
                        style: Ios.subhead(context),
                      ),
                    );
                  }
                  final when = [n.data, n.oraInizio].where((s) => s.isNotEmpty).join(' ');
                  return ListView(
                    padding: const EdgeInsets.fromLTRB(16, 4, 16, 24),
                    children: [
                      Text(n.titolo, style: Ios.title(context)),
                      if (when.isNotEmpty) Text(when, style: Ios.footnote(context)),
                      if (n.riassunto.isNotEmpty)
                        Padding(
                          padding: const EdgeInsets.only(top: 12),
                          child: Text(n.riassunto, style: Ios.body(context)),
                        ),
                      _block(context, tr('Punti chiave', 'Key points'), n.puntiChiave),
                      _block(context, tr('Impegni', 'Commitments'), n.impegni),
                      _block(context, tr('Date ed eventi', 'Dates and events'), n.dateEventi),
                      _block(context, tr('Persone e luoghi', 'People and places'), n.entita),
                    ],
                  );
                },
              ),
            ),
          ],
        ),
      ),
    );
  }
}
