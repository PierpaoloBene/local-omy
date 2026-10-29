import 'package:flutter/cupertino.dart';

import '../api/mac_api.dart';
import '../api/models.dart';
import 'common.dart';
import 'chat.dart';
import 'timeline.dart';

/// The kinds of conversation, as omy transcripts conversations --type.
const conversationTypes = <(String, String, IconData)>[
  ('room', 'Di persona', CupertinoIcons.person_2_fill),
  ('meeting', 'Online', CupertinoIcons.videocam_fill),
  ('call', 'Chiamate', CupertinoIcons.phone_fill),
  ('media', 'Video', CupertinoIcons.play_rectangle_fill),
];

IconData typeIcon(String type) =>
    conversationTypes.firstWhere((t) => t.$1 == type, orElse: () => conversationTypes.first).$3;

/// A row of rounded buttons, one selected (null: the first, "Tutti").
class FilterPills<T> extends StatelessWidget {
  final List<(T, String, IconData?)> values;
  final T? selected;
  final ValueChanged<T?> onChanged;
  final String all;
  const FilterPills({
    super.key,
    required this.values,
    required this.selected,
    required this.onChanged,
    this.all = 'Tutti',
  });

  @override
  Widget build(BuildContext context) {
    Widget pill(T? v, String label, IconData? icon) {
      final on = v == selected;
      final fg = on ? const Color(0xFFFFFFFF) : resolve(context, CupertinoColors.label);
      return Padding(
        padding: const EdgeInsets.only(right: 8),
        child: CupertinoButton(
          padding: const EdgeInsets.symmetric(horizontal: 13),
          minimumSize: const Size(0, 32),
          borderRadius: BorderRadius.circular(16),
          color: on ? resolve(context, tint) : resolve(context, CupertinoColors.secondarySystemGroupedBackground),
          onPressed: () => onChanged(on ? null : v),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              if (icon != null) ...[Icon(icon, size: 15, color: fg), const SizedBox(width: 5)],
              Text(
                label,
                style: Ios.subhead(context).copyWith(color: fg, fontWeight: on ? FontWeight.w600 : null),
              ),
            ],
          ),
        ),
      );
    }

    return SingleChildScrollView(
      scrollDirection: Axis.horizontal,
      padding: const EdgeInsets.symmetric(horizontal: 16),
      child: Row(children: [pill(null, all, null), for (final (v, label, icon) in values) pill(v, label, icon)]),
    );
  }
}

/// A conversation: when and where, who, a sentence; opens it in [list].
class ConversationRow extends StatelessWidget {
  final MacApi api;
  final Conversation c;

  /// The conversations swiped through from this one.
  final List<Conversation> list;
  final String previousPageTitle;
  const ConversationRow({
    super.key,
    required this.api,
    required this.c,
    required this.list,
    required this.previousPageTitle,
  });

  @override
  Widget build(BuildContext context) {
    final others = c.participants.where((p) => !p.isMe).toList();
    final type = c.types.contains('media')
        ? 'media'
        : c.types.contains('call')
        ? 'call'
        : c.types.contains('meeting')
        ? 'meeting'
        : 'room';
    final faces = c.participants.take(3).toList();
    return IosRow(
      leading: SizedBox(
        width: 40,
        height: 40,
        child: Stack(
          children: [
            for (final (i, p) in faces.indexed)
              Positioned(
                left: faces.length == 1 ? 2 : i * 9.0,
                top: faces.length == 1 ? 2 : i * 5.0,
                child: Container(
                  padding: const EdgeInsets.all(1),
                  decoration: BoxDecoration(
                    shape: BoxShape.circle,
                    color: resolve(context, CupertinoColors.secondarySystemGroupedBackground),
                  ),
                  child: PersonAvatar(name: p.name, me: p.isMe, size: faces.length == 1 ? 34 : 24),
                ),
              ),
          ],
        ),
      ),
      title: [
        '${longDay(c.day)} ${hm(c.start)}',
        if (c.app != null) c.app! else if (c.place != null) c.place!,
      ].join(' · '),
      titleStyle: Ios.headline(context).copyWith(fontSize: 15),
      value: duration(c.minutes),
      chevron: true,
      below: Padding(
        padding: const EdgeInsets.only(top: 2),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Icon(typeIcon(type), size: 13, color: resolve(context, CupertinoColors.secondaryLabel)),
                const SizedBox(width: 4),
                Expanded(
                  child: Text(
                    (others.isEmpty ? ['Solo tu'] : others.map((p) => p.name)).join(', '),
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: Ios.footnote(context),
                  ),
                ),
              ],
            ),
            if (c.snippet.isNotEmpty)
              Text(
                '«${c.snippet}»',
                maxLines: 2,
                overflow: TextOverflow.ellipsis,
                style: Ios.footnote(context).copyWith(fontStyle: FontStyle.italic),
              ),
          ],
        ),
      ),
      onTap: () => ConversationViewer.open(context, api, list, list.indexOf(c), previousPageTitle: previousPageTitle),
    );
  }
}

/// Every conversation with a person or of a type, as a timeline.
class ConversationsPage extends StatefulWidget {
  final MacApi api;
  final String title;
  final String? person, type, previousPageTitle;
  const ConversationsPage({
    super.key,
    required this.api,
    required this.title,
    this.person,
    this.type,
    this.previousPageTitle,
  });

  @override
  State<ConversationsPage> createState() => _ConversationsPageState();
}

class _ConversationsPageState extends State<ConversationsPage> {
  ConversationList? _list;
  Object? _error;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    setState(() => _error = null);
    try {
      final l = await widget.api.conversations(person: widget.person, type: widget.type, limit: 5000);
      if (mounted) setState(() => _list = l);
    } on MacError catch (e) {
      if (mounted) setState(() => _error = e);
    }
  }

  @override
  Widget build(BuildContext context) => Timeline(
    api: widget.api,
    title: widget.title,
    previousPageTitle: widget.previousPageTitle,
    onRefresh: _load,
    conversations: _list?.conversations,
    error: _error,
    onRetry: _load,
  );
}

/// Conversations as an inset-grouped list, at most [limit] (and then a row
/// to see them all).
class ConversationSection extends StatelessWidget {
  final MacApi api;
  final ConversationList list;
  final String previousPageTitle;
  final String? header;
  final int? limit;
  final VoidCallback? onMore;
  const ConversationSection({
    super.key,
    required this.api,
    required this.list,
    required this.previousPageTitle,
    this.header,
    this.limit,
    this.onMore,
  });

  @override
  Widget build(BuildContext context) {
    final all = list.conversations;
    final shown = limit == null ? all : all.take(limit!).toList();
    return Section(
      header: header ?? '${all.length}${list.more ? '+' : ''} ${all.length == 1 ? 'conversazione' : 'conversazioni'}',
      footer: limit == null && list.more ? 'Le 100 più recenti.' : null,
      children: [
        if (all.isEmpty) const IosRow(title: 'Nessuna conversazione'),
        for (final c in shown) ConversationRow(api: api, c: c, list: all, previousPageTitle: previousPageTitle),
        if (onMore != null && (all.length > shown.length || list.more)) IosRow(title: 'Mostra tutte', onTap: onMore),
      ],
    );
  }
}
