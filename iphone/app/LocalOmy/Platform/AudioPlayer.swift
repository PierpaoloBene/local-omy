import AVFoundation
import Foundation
import Observation

/// Plays a WAV held in memory (a voice sample from the Mac): nothing is written to disk.
@MainActor @Observable
final class AudioPlayer: NSObject, AVAudioPlayerDelegate {
    private(set) var isPlaying = false
    @ObservationIgnored private var player: AVAudioPlayer?

    /// Starts playing; returns the seconds of audio.
    @discardableResult
    func play(_ data: Data) throws -> TimeInterval {
        stop()
        let session = AVAudioSession.sharedInstance()
        try session.setCategory(.playback)
        try session.setActive(true)
        let p = try AVAudioPlayer(data: data)
        p.delegate = self
        p.play()
        player = p
        isPlaying = true
        return p.duration
    }

    func stop() {
        guard let p = player else { return }
        p.stop()
        finished()
    }

    nonisolated func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) {
        MainActor.assumeIsolated { finished() }
    }

    private func finished() {
        player = nil
        isPlaying = false
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }
}
