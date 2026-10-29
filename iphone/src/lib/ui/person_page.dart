import 'package:flutter/cupertino.dart';

import '../api/mac_api.dart';
import '../api/models.dart';
import 'common.dart';
import 'conversations.dart';
import 'timeline.dart';
import '../l10n.dart';

/// One person (or you, with [person] 'me'): how much and where you talk,
/// and every conversation with them. Pops true after a rename or delete.
class PersonPage extends StatefulWidget {
  final MacApi api;
  final String person;
  final String name;
  final double minutes;
  final int sessions;
  final String? lastSeen;
  final PersonIndex? index;

  /// Every other named person, to warn before merging.
  final List<Person> others;

  const PersonPage({
    super.key,
    required this.api,
    required this.person,
    required this.name,
    required this.minutes,
    required this.sessions,
    this.lastSeen,
    this.index,
    this.others = const [],
  });

  @override
  State<PersonPage> createState() => _PersonPageState();
}

class _PersonPageState extends State<PersonPage> {
  late ConversationFeed _feed = ConversationFeed(widget.api, person: widget.person);
  String? _type;
  bool _busy = false;
  late String _name = widget.name;

  bool get _me => widget.person == 'me';

  @override
  void initState() {
    super.initState();
    _feed.refresh();
  }

  @override
  void dispose() {
    _feed.dispose();
    super.dispose();
  }

  Future<void> _load() => _feed.refresh();

  void _setType(String? t) {
    _feed.dispose();
    setState(() {
      _type = t;
      _feed = ConversationFeed(widget.api, person: widget.person, type: t)..refresh();
    });
  }

  /// The kind of all their voices, when it is one: room or meeting.
  String? get _kind {
    final t = {...?widget.index?.types.map((t) => t == 'call' ? 'meeting' : t)}..remove('media');
    return t.length == 1 ? t.first : null;
  }

  Future<void> _edit() async {
    final r = await showCupertinoSheet<_Edit>(
      context: context,
      scrollableBuilder: (context, controller) => _EditSheet(controller: controller, name: _name, kind: _kind),
    );
    if (r == null || !mounted) return;
    if (r.delete) return _delete();
    final name = r.name.isEmpty ? _name : r.name;
    final kind = r.kind != _kind ? r.kind : null;
    if (name == _name && kind == null) return;
    if (kind == 'media') {
      if (!await confirm(
        context,
        title: tr('$_name è un video?', 'Is $_name a video?'),
        message: tr(
          'Tutte le sue voci diventano «Video», in ogni sessione, e non vengono più riconosciute. '
              'Si cancellano il nome e l\'impronta vocale.',
          'All their voices become “Video”, in every session, and are no longer recognized. '
              'The name and the voice print are deleted.',
        ),
        action: tr('È un video', 'It\'s a video'),
      )) {
        return;
      }
      if (!mounted) return;
      if (await change(
            context,
            (b) => setState(() => _busy = b),
            () => widget.api.setPersonKind(widget.person, 'media'),
          ) &&
          mounted) {
        Navigator.pop(context, true);
      }
      return;
    }
    final merging =
        name != _name && widget.others.any((o) => o.id != widget.person && o.name.toLowerCase() == name.toLowerCase());
    if (merging &&
        !await confirm(
          context,
          title: tr('Unire a $name?', 'Merge with $name?'),
          message: tr(
            'Esiste già $name: le due persone diventano una sola, con le voci di entrambe.',
            '$name already exists: the two become one person, with the voices of both.',
          ),
          action: tr('Unisci', 'Merge'),
          destructive: false,
        )) {
      return;
    }
    if (!mounted) return;
    // The kind first: a rename that merges changes the id.
    final ok = await change(context, (b) => setState(() => _busy = b), () async {
      if (kind != null) await widget.api.setPersonKind(widget.person, kind);
      if (name != _name) await widget.api.renamePerson(widget.person, name);
    });
    if (!ok || !mounted) return;
    if (merging) {
      Navigator.pop(context, true);
    } else {
      setState(() => _name = name);
      _load();
    }
  }

  Future<void> _delete() async {
    if (!await confirm(
      context,
      title: tr('Eliminare $_name?', 'Delete $_name?'),
      message: tr(
        'Si cancellano il nome e l\'impronta vocale. Le sue voci tornano senza nome e si possono nominare di nuovo.',
        'The name and the voice print are deleted. Their voices go back to unnamed and can be named again.',
      ),
    )) {
      return;
    }
    if (!mounted) return;
    if (await change(context, (b) => setState(() => _busy = b), () => widget.api.deletePerson(widget.person)) &&
        mounted) {
      Navigator.pop(context, true);
    }
  }

  @override
  Widget build(BuildContext context) {
    final i = widget.index;
    return ListenableBuilder(
      listenable: _feed,
      builder: (context, _) => Timeline(
        api: widget.api,
        title: _name,
        previousPageTitle: tr('Persone', 'People'),
        busy: _busy,
        onRefresh: _load,
        conversations: _feed.list?.conversations,
        hasMore: _feed.hasMore,
        onMore: _feed.more,
        error: _feed.error,
        onRetry: _load,
        trailing: _me
            ? null
            : CupertinoButton(
                padding: EdgeInsets.zero,
                onPressed: _busy ? null : _edit,
                child: Text(tr('Modifica', 'Edit')),
              ),
        top: [
          SliverList.list(
            children: [
              InsetCard(
                child: Row(
                  children: [
                    PersonAvatar(name: _name, me: _me, size: 64),
                    const SizedBox(width: 16),
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(
                            '${duration(widget.minutes)} · ${counted(widget.sessions, 'sessione', 'sessioni', 'session', 'sessions')}',
                            style: Ios.headline(context),
                          ),
                          if (widget.lastSeen != null)
                            Text(
                              tr(
                                'Ultima volta ${longDay(widget.lastSeen!).toLowerCase()}',
                                'Last seen ${longDay(widget.lastSeen!)}',
                              ),
                              style: Ios.footnote(context),
                            ),
                          if (i != null && i.places.isNotEmpty)
                            Padding(
                              padding: const EdgeInsets.only(top: 4),
                              child: Row(
                                children: [
                                  Icon(
                                    CupertinoIcons.location_solid,
                                    size: 13,
                                    color: resolve(context, CupertinoColors.secondaryLabel),
                                  ),
                                  const SizedBox(width: 4),
                                  Expanded(child: Text(i.places.join(', '), style: Ios.footnote(context))),
                                ],
                              ),
                            ),
                        ],
                      ),
                    ),
                  ],
                ),
              ),
              const SizedBox(height: 16),
              FilterPills<String>(
                values: [
                  for (final t in conversationTypes)
                    if (i == null || _me || i.types.contains(t.$1)) t,
                ],
                selected: _type,
                onChanged: _setType,
              ),
              const SizedBox(height: 12),
            ],
          ),
        ],
      ),
    );
  }
}

typedef _Edit = ({String name, String? kind, bool delete});

/// Name and kind of a person, like editing a contact; delete at the bottom.
class _EditSheet extends StatefulWidget {
  final ScrollController controller;
  final String name;
  final String? kind;
  const _EditSheet({required this.controller, required this.name, required this.kind});

  @override
  State<_EditSheet> createState() => _EditSheetState();
}

class _EditSheetState extends State<_EditSheet> {
  late final _c = TextEditingController(text: widget.name);
  late String? _kind = widget.kind;

  @override
  void dispose() {
    _c.dispose();
    super.dispose();
  }

  void _done({bool delete = false}) =>
      Navigator.pop<_Edit>(context, (name: _c.text.trim(), kind: _kind, delete: delete));

  @override
  Widget build(BuildContext context) => SheetPage(
    controller: widget.controller,
    title: tr('Modifica', 'Edit'),
    action: tr('Fine', 'Done'),
    onAction: _done,
    children: [
      CupertinoFormSection.insetGrouped(
        header: const Text('NOME'),
        children: [
          CupertinoTextFormFieldRow(
            controller: _c,
            placeholder: tr('Nome', 'Name'),
            textCapitalization: TextCapitalization.words,
            onFieldSubmitted: (_) => _done(),
          ),
        ],
      ),
      Padding(
        padding: const EdgeInsets.fromLTRB(32, 16, 32, 8),
        child: Text('TIPO', style: Ios.footnote(context)),
      ),
      Padding(
        padding: const EdgeInsets.symmetric(horizontal: 16),
        child: CupertinoSlidingSegmentedControl<String>(
          groupValue: _kind,
          onValueChanged: (k) => setState(() => _kind = k),
          children: {
            'room': Padding(
              padding: const EdgeInsets.symmetric(vertical: 6),
              child: Text(tr('Di persona', 'In person')),
            ),
            'meeting': const Padding(padding: EdgeInsets.symmetric(vertical: 6), child: Text('Online')),
            'media': const Padding(padding: EdgeInsets.symmetric(vertical: 6), child: Text('Video')),
          },
        ),
      ),
      Padding(
        padding: const EdgeInsets.fromLTRB(32, 8, 32, 0),
        child: Text(switch (_kind) {
          'media' => tr(
            'Era un video: tutte le sue voci diventano «Video», e il nome e l\'impronta vocale si cancellano.',
            'It was a video: all their voices become “Video”, and the name and voice print are deleted.',
          ),
          'meeting' => tr(
            'Tutte le sue voci: in una chiamata o una riunione online.',
            'All their voices: in a call or an online meeting.',
          ),
          'room' => tr(
            'Tutte le sue voci: di persona, nella stanza con te.',
            'All their voices: in person, in the room with you.',
          ),
          _ => tr(
            'Le sue voci sono di tipi diversi: scegline uno per cambiarle tutte.',
            'Their voices are of different types: choose one to change them all.',
          ),
        }, style: Ios.footnote(context)),
      ),
      const SizedBox(height: 24),
      Section(
        children: [
          IosRow(title: tr('Elimina persona', 'Delete person'), destructive: true, onTap: () => _done(delete: true)),
        ],
      ),
    ],
  );
}
