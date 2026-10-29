import 'package:flutter/cupertino.dart';

import '../api/mac_api.dart';
import '../api/models.dart';
import 'common.dart';
import 'conversations.dart';
import 'timeline.dart';

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
  ConversationList? _list;
  Object? _error;
  String? _type;
  bool _busy = false;
  late String _name = widget.name;

  bool get _me => widget.person == 'me';

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    setState(() => _error = null);
    try {
      final l = await widget.api.conversations(person: widget.person, type: _type, limit: 5000);
      if (mounted) setState(() => _list = l);
    } on MacError catch (e) {
      if (mounted) setState(() => _error = e);
    }
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
        title: '$_name è un video?',
        message:
            'Tutte le sue voci diventano «Video», in ogni sessione, e non vengono più riconosciute. '
            'Si cancellano il nome e l\'impronta vocale.',
        action: 'È un video',
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
          title: 'Unire a $name?',
          message: 'Esiste già $name: le due persone diventano una sola, con le voci di entrambe.',
          action: 'Unisci',
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
      title: 'Eliminare $_name?',
      message:
          'Si cancellano il nome e l\'impronta vocale. Le sue voci tornano senza nome e si possono nominare di nuovo.',
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
    return Timeline(
      api: widget.api,
      title: _name,
      previousPageTitle: 'Persone',
      busy: _busy,
      onRefresh: _load,
      conversations: _list?.conversations,
      error: _error,
      onRetry: _load,
      trailing: _me
          ? null
          : CupertinoButton(padding: EdgeInsets.zero, onPressed: _busy ? null : _edit, child: const Text('Modifica')),
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
                          '${duration(widget.minutes)} · ${widget.sessions} ${widget.sessions == 1 ? 'sessione' : 'sessioni'}',
                          style: Ios.headline(context),
                        ),
                        if (widget.lastSeen != null)
                          Text('Ultima volta ${longDay(widget.lastSeen!).toLowerCase()}', style: Ios.footnote(context)),
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
              onChanged: (t) {
                setState(() {
                  _type = t;
                  _list = null;
                });
                _load();
              },
            ),
            const SizedBox(height: 12),
          ],
        ),
      ],
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
    title: 'Modifica',
    action: 'Fine',
    onAction: _done,
    children: [
      CupertinoFormSection.insetGrouped(
        header: const Text('NOME'),
        children: [
          CupertinoTextFormFieldRow(
            controller: _c,
            placeholder: 'Nome',
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
          children: const {
            'room': Padding(padding: EdgeInsets.symmetric(vertical: 6), child: Text('Di persona')),
            'meeting': Padding(padding: EdgeInsets.symmetric(vertical: 6), child: Text('Online')),
            'media': Padding(padding: EdgeInsets.symmetric(vertical: 6), child: Text('Video')),
          },
        ),
      ),
      Padding(
        padding: const EdgeInsets.fromLTRB(32, 8, 32, 0),
        child: Text(switch (_kind) {
          'media' => 'Era un video: tutte le sue voci diventano «Video», e il nome e l\'impronta vocale si cancellano.',
          'meeting' => 'Tutte le sue voci: in una chiamata o una riunione online.',
          'room' => 'Tutte le sue voci: di persona, nella stanza con te.',
          _ => 'Le sue voci sono di tipi diversi: scegline uno per cambiarle tutte.',
        }, style: Ios.footnote(context)),
      ),
      const SizedBox(height: 24),
      Section(
        children: [IosRow(title: 'Elimina persona', destructive: true, onTap: () => _done(delete: true))],
      ),
    ],
  );
}
