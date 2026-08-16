/*  The chime — a port of chime() in web/app.js.
 *
 *  Same shape: a square wave that steps 880 Hz → 1180 Hz 150 ms in, with a
 *  sharp attack and an exponential decay, repeated every 400 ms. Five for a
 *  confirmed dispatch, two for an unconfirmed one.
 *
 *  Category .ambient, deliberately: it respects the ring/silent switch and does
 *  not duck other audio, which is how WebAudio behaves in Safari. The chime is
 *  the acknowledgement that something arrived while you are looking at the app.
 *  A notification that must pierce silence is a push, not this.
 */

import AVFoundation

final class Chime {
    static let shared = Chime()

    private let engine = AVAudioEngine()
    private let player = AVAudioPlayerNode()
    private var prepared = false

    private let sampleRate: Double = 44_100
    private let noteGap: Double = 0.4
    private let noteLength: Double = 0.36

    private init() {}

    /// Called on the user gesture that turns sound on, so the first real alert
    /// is not the thing that has to warm the audio stack up.
    private func prepare() throws {
        guard !prepared else { return }
        let session = AVAudioSession.sharedInstance()
        try session.setCategory(.ambient, mode: .default, options: [.mixWithOthers])
        try session.setActive(true)

        guard let format = AVAudioFormat(standardFormatWithSampleRate: sampleRate,
                                         channels: 1) else { return }
        engine.attach(player)
        engine.connect(player, to: engine.mainMixerNode, format: format)
        try engine.start()
        prepared = true
    }

    func play(times: Int = 3) {
        do {
            try prepare()
            guard let buffer = makeBuffer(times: max(1, times)) else { return }
            if !player.isPlaying { player.play() }
            player.scheduleBuffer(buffer, at: nil, options: [], completionHandler: nil)
        } catch {
            // Audio session refusals (a call in progress, another app holding
            // exclusive audio) are not worth surfacing: the dispatch still
            // lands on screen, which is the part that matters.
        }
    }

    private func makeBuffer(times: Int) -> AVAudioPCMBuffer? {
        guard let format = AVAudioFormat(standardFormatWithSampleRate: sampleRate,
                                         channels: 1) else { return nil }
        let total = noteGap * Double(times - 1) + noteLength
        let frames = AVAudioFrameCount(total * sampleRate)
        guard let buffer = AVAudioPCMBuffer(pcmFormat: format,
                                            frameCapacity: frames) else { return nil }
        buffer.frameLength = frames
        guard let channel = buffer.floatChannelData?[0] else { return nil }

        for f in 0..<Int(frames) { channel[f] = 0 }

        // Phase is accumulated rather than derived from t, so the 880→1180 step
        // does not produce a click where the two sines disagree.
        for note in 0..<times {
            let start = noteGap * Double(note)
            var phase = 0.0
            var f = Int(start * sampleRate)
            let end = min(Int((start + noteLength) * sampleRate), Int(frames))
            while f < end {
                let t = Double(f) / sampleRate - start
                let freq = t < 0.15 ? 880.0 : 1180.0
                phase += 2 * Double.pi * freq / sampleRate
                // Square wave, as in the web client's osc.type = 'square'.
                let square: Double = sin(phase) >= 0 ? 1 : -1
                channel[f] += Float(square * envelope(t) * 0.25)
                f += 1
            }
        }
        return buffer
    }

    /// 0.0001 → 0.25 over 20 ms, then back to 0.0001 by 340 ms — the two
    /// exponential ramps the web client schedules on its gain node, normalised
    /// so the peak is 1.0 and the caller applies the 0.25.
    private func envelope(_ t: Double) -> Double {
        let peak = 0.02, tail = 0.34
        if t < 0 { return 0 }
        if t < peak { return pow(2500, t / peak) / 2500 }
        if t < tail { return pow(2500, -(t - peak) / (tail - peak)) }
        return 0
    }
}
