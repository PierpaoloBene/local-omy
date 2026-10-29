/// Places on Apple's maps (AppDelegate.swift, Maps): a picture of the spot
/// with its radius, the address, the Maps app. Kept in memory only, while
/// the app runs.
library;

import 'package:flutter/cupertino.dart';
import 'package:flutter/services.dart';

import '../ui/common.dart';

abstract final class Maps {
  static const _channel = MethodChannel('local_omy/maps');
  static final _pictures = <String, Future<Uint8List?>>{};
  static final _addresses = <String, Future<String?>>{};

  /// A failure (offline) is not kept: the next build tries again.
  static Future<Uint8List?> picture(double lat, double lon, int radius, Size size, bool dark) {
    final key = '$lat,$lon,$radius,${size.width},${size.height},$dark';
    return _pictures.putIfAbsent(key, () async {
      try {
        return await _channel.invokeMethod<Uint8List>('snapshot', {
          'lat': lat,
          'lon': lon,
          'radius': radius.toDouble(),
          'width': size.width,
          'height': size.height,
          'dark': dark,
        });
      } on Exception {
        _pictures.remove(key);
        return null;
      }
    });
  }

  static Future<String?> address(double lat, double lon) => _addresses.putIfAbsent('$lat,$lon', () async {
    try {
      return await _channel.invokeMethod<String>('address', {'lat': lat, 'lon': lon});
    } on Exception {
      return null;
    }
  });

  static Future<void> open(double lat, double lon, String? name) =>
      _channel.invokeMethod('open', {'lat': lat, 'lon': lon, 'name': name});
}

/// The map of a spot, rounded; tap to open it in Maps.
class MapPicture extends StatelessWidget {
  final double lat, lon;
  final int radius;
  final double height;
  final String? name;
  final BorderRadius borderRadius;
  const MapPicture({
    super.key,
    required this.lat,
    required this.lon,
    required this.radius,
    this.height = 150,
    this.name,
    this.borderRadius = const BorderRadius.all(Radius.circular(10)),
  });

  @override
  Widget build(BuildContext context) => LayoutBuilder(
    builder: (context, c) {
      final size = Size(c.maxWidth.isFinite ? c.maxWidth : height, height);
      final dark = CupertinoTheme.brightnessOf(context) == Brightness.dark;
      return GestureDetector(
        onTap: () => Maps.open(lat, lon, name),
        child: ClipRRect(
          borderRadius: borderRadius,
          child: SizedBox.fromSize(
            size: size,
            child: FutureBuilder(
              future: Maps.picture(lat, lon, radius, size, dark),
              builder: (context, snap) => snap.data != null
                  ? Image.memory(snap.data!, fit: BoxFit.cover, gaplessPlayback: true)
                  : Container(
                      color: resolve(context, CupertinoColors.tertiarySystemFill),
                      alignment: Alignment.center,
                      child: snap.connectionState == ConnectionState.done
                          ? Icon(CupertinoIcons.map, color: resolve(context, CupertinoColors.tertiaryLabel))
                          : const CupertinoActivityIndicator(),
                    ),
            ),
          ),
        ),
      );
    },
  );
}

/// The address of a spot, gray while it loads.
class AddressText extends StatelessWidget {
  final double lat, lon;
  final TextStyle? style;
  final String fallback;
  const AddressText({super.key, required this.lat, required this.lon, this.style, this.fallback = ''});

  @override
  Widget build(BuildContext context) => FutureBuilder(
    future: Maps.address(lat, lon),
    builder: (context, snap) => Text(
      snap.data ?? (snap.connectionState == ConnectionState.done ? fallback : 'Cerco l\'indirizzo…'),
      style: style,
      maxLines: 2,
      overflow: TextOverflow.ellipsis,
    ),
  );
}
