import 'package:flutter/cupertino.dart';

import '../api/mac_api.dart';
import '../api/models.dart';
import '../features.dart';
import 'app_shell.dart';
import 'common.dart';

const _radii = [50, 100, 150, 300, 500, 1000];

/// "Luoghi": names for where you record, from the phone's positions. No map:
/// coordinates are never sent to a map service.
class PlacesPage extends StatefulWidget {
  final MacApi api;
  final Features features;
  final TabShown shown;
  const PlacesPage({super.key, required this.api, required this.features, required this.shown});

  @override
  State<PlacesPage> createState() => _PlacesPageState();
}

class _PlacesPageState extends State<PlacesPage> {
  Places? _places;
  Object? _error;
  bool _busy = false;

  @override
  void initState() {
    super.initState();
    widget.shown.addListener(_load);
    _load();
  }

  @override
  void dispose() {
    widget.shown.removeListener(_load);
    super.dispose();
  }

  Future<void> _load() async {
    setState(() => _error = null);
    try {
      final p = await widget.api.places();
      if (mounted) setState(() => _places = p);
    } on MacError catch (e) {
      if (mounted) setState(() => _error = e);
    }
  }

  Future<void> _change(Future<void> Function() body) async {
    if (await change(context, (b) => setState(() => _busy = b), body)) await _load();
  }

  bool _taken(String name, {String? except}) => _places!.places.any(
    (p) => p.name.toLowerCase() == name.toLowerCase() && p.name.toLowerCase() != except?.toLowerCase(),
  );

  Future<void> _name(Spot s) async {
    final r = await showCupertinoSheet<(String, int)>(
      context: context,
      scrollableBuilder: (context, controller) => _PlaceSheet(spot: s, controller: controller),
    );
    if (r == null || r.$1.isEmpty || !mounted) return;
    // A place has one center: the same name would move it here.
    if (_taken(r.$1)) {
      showMessage(context, 'Scegli un altro nome, o allarga il raggio di ${r.$1}.', title: '${r.$1} esiste già');
      return;
    }
    await _change(() => widget.api.addPlace(r.$1, s.lat, s.lon, r.$2));
  }

  Future<void> _actions(NamedPlace p) async {
    final a = await chooseAction(
      context,
      title: p.name,
      message: 'Raggio ${p.radius} m',
      actions: [('rename', 'Rinomina', false), ('radius', 'Cambia raggio', false), ('remove', 'Elimina', true)],
    );
    if (!mounted) return;
    switch (a) {
      case 'rename':
        await _rename(p);
      case 'radius':
        await _radius(p);
      case 'remove':
        await _remove(p);
    }
  }

  Future<void> _rename(NamedPlace p) async {
    final n = await askName(context, title: 'Rinomina', initial: p.name);
    if (n == null || n.isEmpty || n == p.name || !mounted) return;
    if (_taken(n, except: p.name)) {
      showMessage(context, 'Scegli un altro nome.', title: '$n esiste già');
      return;
    }
    await _change(() => widget.api.renamePlace(p.name, n));
  }

  Future<void> _radius(NamedPlace p) async {
    final r = await chooseAction(
      context,
      title: 'Raggio di ${p.name}',
      actions: [for (final r in _radii) (r, r == p.radius ? '$r m ✓' : '$r m', false)],
    );
    // Same name and center: the Mac replaces it.
    if (r != null && r != p.radius) await _change(() => widget.api.addPlace(p.name, p.lat, p.lon, r));
  }

  Future<void> _remove(NamedPlace p) async {
    if (await confirm(
      context,
      title: 'Eliminare ${p.name}?',
      message: 'Si cancella solo il nome: le posizioni restano, e nelle trascrizioni tornano coordinate.',
    )) {
      await _change(() => widget.api.removePlace(p.name));
    }
  }

  @override
  Widget build(BuildContext context) {
    final p = _places;
    return TabScaffold(
      title: 'Luoghi',
      onRefresh: _load,
      busy: _busy,
      slivers: p == null
          ? [LoadState(loading: _error == null, error: _error, onRetry: _load)]
          : [
              SliverList.list(
                children: [
                  if (!widget.features.on('location'))
                    const Notice(
                      icon: CupertinoIcons.info_circle_fill,
                      text:
                          'Posizione disattivata: le registrazioni nuove non hanno il luogo (Impostazioni, Funzioni).',
                    ),
                  Section(
                    header: 'Con un nome',
                    children: p.places.isEmpty
                        ? [const IosRow(title: 'Nessuno', subtitle: 'Dai un nome a uno dei punti qui sotto.')]
                        : [for (final place in p.places) _placeRow(context, place)],
                  ),
                  const SizedBox(height: 12),
                  Section(
                    header: 'Dove hai registrato, senza un nome',
                    footer: 'I punti dove hai passato più tempo. Le posizioni arrivano con i segmenti.',
                    children: p.unnamed.isEmpty
                        ? [const IosRow(title: 'Nessun punto senza nome')]
                        : [for (final s in p.unnamed) _spotRow(context, s)],
                  ),
                ],
              ),
            ],
    );
  }

  Widget _placeRow(BuildContext context, NamedPlace p) => IosRow(
    leading: const IconSquare(CupertinoIcons.location_solid, CupertinoColors.systemBlue),
    title: p.name,
    subtitle:
        'Raggio ${p.radius} m · ${duration(p.minutes)}${p.lastSeen == null ? '' : ' · ultima ${shortDay(p.lastSeen!)}'}',
    trailing: Icon(CupertinoIcons.ellipsis_circle, color: resolve(context, tint)),
    onTap: _busy ? null : () => _actions(p),
  );

  Widget _spotRow(BuildContext context, Spot s) => IosRow(
    leading: const IconSquare(CupertinoIcons.location, CupertinoColors.systemGrey),
    title: '${duration(s.minutes)} · ${s.days == 1 ? '1 giorno' : '${s.days} giorni'}',
    subtitle: _details(s),
    below: s.sample.isEmpty
        ? null
        : Padding(
            padding: const EdgeInsets.only(top: 3),
            child: Text(
              '«${s.sample}»',
              maxLines: 3,
              overflow: TextOverflow.ellipsis,
              style: Ios.footnote(context).copyWith(fontStyle: FontStyle.italic),
            ),
          ),
    trailing: CupertinoButton(
      padding: EdgeInsets.zero,
      minimumSize: const Size(44, 44),
      onPressed: _busy ? null : () => _name(s),
      child: const Text('Nomina'),
    ),
  );
}

String _details(Spot s) => [
  s.from == s.to ? 'alle ${s.from}' : 'tra le ${s.from} e le ${s.to}',
  if (s.lastSeen != null) 'ultima ${shortDay(s.lastSeen!)}',
  if (s.nearest != null) 'a ${meters(s.nearest!.meters)} da ${s.nearest!.name}',
].join(' · ');

class _PlaceSheet extends StatefulWidget {
  final Spot spot;
  final ScrollController controller;
  const _PlaceSheet({required this.spot, required this.controller});

  @override
  State<_PlaceSheet> createState() => _PlaceSheetState();
}

class _PlaceSheetState extends State<_PlaceSheet> {
  final _c = TextEditingController();
  int _radius = 150;

  @override
  void dispose() {
    _c.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => SheetPage(
    controller: widget.controller,
    title: 'Nuovo luogo',
    action: 'Salva',
    onAction: () => Navigator.pop(context, (_c.text.trim(), _radius)),
    children: [
      Padding(
        padding: const EdgeInsets.fromLTRB(32, 0, 32, 12),
        child: Text(_details(widget.spot), style: Ios.footnote(context)),
      ),
      CupertinoFormSection.insetGrouped(
        children: [
          CupertinoTextFormFieldRow(
            controller: _c,
            autofocus: true,
            placeholder: 'Casa, Ufficio, Palestra…',
            textCapitalization: TextCapitalization.sentences,
            onFieldSubmitted: (v) => Navigator.pop(context, (v.trim(), _radius)),
          ),
        ],
      ),
      Padding(
        padding: const EdgeInsets.fromLTRB(32, 16, 32, 8),
        child: Text('RAGGIO', style: Ios.footnote(context)),
      ),
      Padding(
        padding: const EdgeInsets.symmetric(horizontal: 16),
        child: CupertinoSlidingSegmentedControl<int>(
          groupValue: _radius,
          onValueChanged: (v) => setState(() => _radius = v ?? _radius),
          children: {for (final r in _radii.take(5)) r: Text('$r m')},
        ),
      ),
      Padding(
        padding: const EdgeInsets.fromLTRB(32, 8, 32, 0),
        child: Text(
          'Le registrazioni entro questa distanza dal punto prendono il nome del luogo.',
          style: Ios.footnote(context),
        ),
      ),
    ],
  );
}
