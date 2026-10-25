// SPDX-License-Identifier: AGPL-3.0-only
import CoreAudio
import AVFoundation
import Foundation
import Synchronization

#if !_endian(little)
#error("PLH1 sample payloads are copied as little-endian float32")
#endif

// BEGIN DEVICE-FREE
struct BlockRecord {
    var sampleTime: Double
    var hostTime: UInt64
    var flags: UInt32
    var frames: UInt32
    var ringStart: UInt64
    var cbStart: UInt64
    var cbEnd: UInt64
    var droppedFrames: UInt64
    var droppedBlocks: UInt64
}

struct ChannelSource { var buffer: Int32; var offset: Int32; var stride: Int32 }

struct RTContext {
    var samples: UnsafeMutablePointer<Float>
    var frameCapacity: UInt64
    var channels: Int
    var sources: UnsafeMutablePointer<ChannelSource>
    var expectedBuffers: UInt32
    var expectedBufferChannels: UnsafeMutablePointer<UInt32>
    var records: UnsafeMutablePointer<BlockRecord>
    var recordCapacity: UInt64
    var maxBlockFrames: UInt32
    var counters: UnsafeMutablePointer<Atomic<UInt64>>
}

enum Counter: Int, CaseIterable {
    case framesWritten, framesRead, blocksWritten, blocksRead, droppedFrames, droppedBlocks,
         oversizeBlocks, layoutMismatchBlocks, callbacks, callbackMaxTicks
}

@inline(__always) func counter(_ ctx: UnsafeMutablePointer<RTContext>, _ c: Counter) -> UnsafeMutablePointer<Atomic<UInt64>> {
    ctx.pointee.counters + c.rawValue
}

@inline(__always) func value(_ ctx: UnsafeMutablePointer<RTContext>, _ c: Counter) -> UInt64 {
    counter(ctx, c).pointee.load(ordering: .acquiring)
}

let recordCapacity: UInt64 = 4096

func powerOfTwo(atLeast n: UInt64) -> UInt64 {
    var v: UInt64 = 1
    while v < n { v <<= 1 }
    return v
}

func makeContext(sources: [ChannelSource], bufferChannels: [UInt32], frames: UInt64,
                 maxBlockFrames: UInt32) -> UnsafeMutablePointer<RTContext> {
    let channels = sources.count
    let capacity = powerOfTwo(atLeast: max(frames, 1))
    let samples = UnsafeMutablePointer<Float>.allocate(capacity: Int(capacity) * channels)
    samples.initialize(repeating: 0, count: Int(capacity) * channels)
    let table = UnsafeMutablePointer<ChannelSource>.allocate(capacity: channels)
    table.initialize(from: sources, count: channels)
    let expected = UnsafeMutablePointer<UInt32>.allocate(capacity: max(bufferChannels.count, 1))
    expected.initialize(repeating: 0, count: max(bufferChannels.count, 1))
    for (i, n) in bufferChannels.enumerated() { expected[i] = n }
    let records = UnsafeMutablePointer<BlockRecord>.allocate(capacity: Int(recordCapacity))
    records.initialize(repeating: BlockRecord(sampleTime: 0, hostTime: 0, flags: 0, frames: 0, ringStart: 0,
                                              cbStart: 0, cbEnd: 0, droppedFrames: 0, droppedBlocks: 0),
                       count: Int(recordCapacity))
    let counters = UnsafeMutablePointer<Atomic<UInt64>>.allocate(capacity: Counter.allCases.count)
    for i in 0..<Counter.allCases.count { (counters + i).initialize(to: Atomic<UInt64>(0)) }
    let ctx = UnsafeMutablePointer<RTContext>.allocate(capacity: 1)
    ctx.initialize(to: RTContext(samples: samples, frameCapacity: capacity, channels: channels, sources: table,
                                 expectedBuffers: UInt32(bufferChannels.count), expectedBufferChannels: expected,
                                 records: records, recordCapacity: recordCapacity, maxBlockFrames: maxBlockFrames,
                                 counters: counters))
    return ctx
}

func put<T: FixedWidthInteger>(_ bytes: inout [UInt8], _ v: T) {
    withUnsafeBytes(of: v.littleEndian) { bytes.append(contentsOf: $0) }
}

func writeAll(_ bytes: [UInt8]) -> Bool {
    bytes.withUnsafeBytes { raw in
        var offset = 0
        while offset < raw.count {
            let n = Darwin.write(1, raw.baseAddress! + offset, raw.count - offset)
            if n < 0 && errno == EINTR { continue }
            if n <= 0 { return false }
            offset += n
        }
        return true
    }
}

final class Wire {
    private var serial: UInt32 = 0
    private(set) var failed = false

    func send(_ type: UInt16, _ payload: [UInt8]) -> Bool {
        if failed { return false }
        var frame: [UInt8] = [0x50, 0x4C, 0x48, 0x31]
        frame.reserveCapacity(16 + payload.count)
        put(&frame, type); put(&frame, UInt16(0)); put(&frame, UInt32(payload.count)); put(&frame, serial)
        frame.append(contentsOf: payload)
        serial &+= 1
        if !writeAll(frame) { failed = true }
        return !failed
    }

    func json(_ type: UInt16, _ schema: String, _ fields: [String: Any]) -> Bool {
        let object = fields.merging(["schema": schema]) { _, new in new }
        if JSONSerialization.isValidJSONObject(object),
           let data = try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys]) {
            return send(type, [UInt8](data))
        }
        return send(type, [UInt8]("{\"encoding_error\":true,\"schema\":\"\(schema)\"}".utf8))
    }
}

let helloSchema = "pocket.listen-helper-hello/v1", startedSchema = "pocket.listen-helper-started/v1"
let eventSchema = "pocket.listen-helper-event/v1", exitSchema = "pocket.listen-helper-exit/v1"
let frameHello: UInt16 = 1, frameBlock: UInt16 = 2, frameEvent: UInt16 = 3, frameHeartbeat: UInt16 = 4
let frameExit: UInt16 = 5, frameStarted: UInt16 = 6

let drainMaxBlocks: UInt64 = 64, drainBudgetMs: UInt64 = 50
func drainOnce(_ ctx: UnsafeMutablePointer<RTContext>, _ wire: Wire) -> Int {
    var sent = 0
    let channels = ctx.pointee.channels
    let mask = ctx.pointee.frameCapacity &- 1
    let began = DispatchTime.now().uptimeNanoseconds
    let first = counter(ctx, .blocksRead).pointee.load(ordering: .relaxed)
    let end = min(counter(ctx, .blocksWritten).pointee.load(ordering: .acquiring), first &+ drainMaxBlocks)
    while true {
        let br = counter(ctx, .blocksRead).pointee.load(ordering: .relaxed)
        if br == end || (sent > 0 && elapsedMs(since: began) >= drainBudgetMs) { return sent }
        let r = ctx.pointee.records[Int(br & (ctx.pointee.recordCapacity &- 1))]
        let frames = Int(r.frames)
        var payload: [UInt8] = []
        payload.reserveCapacity(72 + frames * channels * 4)
        put(&payload, br); put(&payload, r.sampleTime.bitPattern); put(&payload, r.hostTime)
        put(&payload, r.flags); put(&payload, r.frames); put(&payload, UInt16(channels)); put(&payload, UInt16(0))
        put(&payload, UInt32(0)); put(&payload, r.droppedFrames); put(&payload, r.droppedBlocks)
        put(&payload, r.cbStart); put(&payload, r.cbEnd)
        let start = Int(r.ringStart & mask)
        let first = min(frames, Int(ctx.pointee.frameCapacity) - start)
        payload.append(contentsOf: UnsafeRawBufferPointer(start: ctx.pointee.samples + start * channels,
                                                          count: first * channels * 4))
        if first < frames {
            payload.append(contentsOf: UnsafeRawBufferPointer(start: ctx.pointee.samples,
                                                              count: (frames - first) * channels * 4))
        }
        if !wire.send(frameBlock, payload) { return -1 }
        counter(ctx, .framesRead).pointee.store(r.ringStart &+ UInt64(r.frames), ordering: .releasing)
        counter(ctx, .blocksRead).pointee.store(br &+ 1, ordering: .releasing)
        sent += 1
    }
}

func heartbeat(_ ctx: UnsafeMutablePointer<RTContext>, _ wire: Wire) -> Bool {
    var payload: [UInt8] = []
    put(&payload, mach_absolute_time()); put(&payload, value(ctx, .blocksWritten))
    put(&payload, value(ctx, .droppedFrames))
    put(&payload, counter(ctx, .callbackMaxTicks).pointee.exchange(0, ordering: .relaxed))
    put(&payload, value(ctx, .callbacks))
    return wire.send(frameHeartbeat, payload)
}

func counters(_ ctx: UnsafeMutablePointer<RTContext>) -> [String: Any] {
    ["callbacks": value(ctx, .callbacks), "blocks_written": value(ctx, .blocksWritten),
     "blocks_sent": value(ctx, .blocksRead), "dropped_frames": value(ctx, .droppedFrames),
     "dropped_blocks": value(ctx, .droppedBlocks), "oversize_blocks": value(ctx, .oversizeBlocks),
     "layout_mismatch_blocks": value(ctx, .layoutMismatchBlocks)]
}

func timebase() -> [String: Any] {
    var info = mach_timebase_info_data_t()
    mach_timebase_info(&info)
    return ["numer": Int(info.numer), "denom": Int(info.denom)]
}

func macOSVersion() -> String {
    let v = ProcessInfo.processInfo.operatingSystemVersion
    return "\(v.majorVersion).\(v.minorVersion).\(v.patchVersion)"
}

func elapsedMs(since start: UInt64) -> UInt64 {
    (DispatchTime.now().uptimeNanoseconds &- start) / 1_000_000
}
func usage(_ message: String) -> Int32 {
    FileHandle.standardError.write(Data(("listen-capture: " + message + "\n").utf8))
    return 2
}

func options(_ args: ArraySlice<String>, _ allowed: Set<String>) -> [String: String]? {
    var result: [String: String] = [:]
    var i = args.startIndex
    while i < args.endIndex {
        let key = args[i]
        guard key.hasPrefix("--"), allowed.contains(String(key.dropFirst(2))), i + 1 < args.endIndex,
              result[String(key.dropFirst(2))] == nil else { return nil }
        result[String(key.dropFirst(2))] = args[i + 1]
        i += 2
    }
    return result
}

func integer(_ options: [String: String], _ key: String, _ fallback: Int?, _ range: ClosedRange<Int>) -> Int? {
    guard let text = options[key] else { return fallback }
    guard let n = Int(text), range.contains(n) else { return nil }
    return n
}
// END DEVICE-FREE

// BEGIN REALTIME
let listenIOProc: AudioDeviceIOProc = { _, _, inInputData, inInputTime, _, _, inClientData in
    let entry = mach_absolute_time()
    guard let raw = inClientData else { return noErr }
    let ctx = raw.assumingMemoryBound(to: RTContext.self)
    counter(ctx, .callbacks).pointee.add(1, ordering: .relaxed)
    let abl = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: inInputData))
    let buffers = Int(ctx.pointee.expectedBuffers)
    if buffers == 0 || abl.count != buffers || abl[0].mNumberChannels == 0 {
        counter(ctx, .layoutMismatchBlocks).pointee.add(1, ordering: .relaxed)
        return noErr
    }
    let frames = Int(abl[0].mDataByteSize) / (4 * Int(abl[0].mNumberChannels))
    var b = 0
    while b < buffers {
        let buffer = abl[b]
        if buffer.mNumberChannels != ctx.pointee.expectedBufferChannels[b]
            || Int(buffer.mDataByteSize) != frames * 4 * Int(buffer.mNumberChannels) {
            counter(ctx, .layoutMismatchBlocks).pointee.add(1, ordering: .relaxed)
            return noErr
        }
        b += 1
    }
    if frames == 0 { return noErr }
    let channels = ctx.pointee.channels
    var c = 0
    while c < channels {
        if abl[Int(ctx.pointee.sources[c].buffer)].mData == nil {
            counter(ctx, .layoutMismatchBlocks).pointee.add(1, ordering: .relaxed)
            return noErr
        }
        c += 1
    }
    if frames > Int(ctx.pointee.maxBlockFrames) {
        counter(ctx, .oversizeBlocks).pointee.add(1, ordering: .relaxed)
        counter(ctx, .droppedFrames).pointee.add(UInt64(frames), ordering: .relaxed)
        counter(ctx, .droppedBlocks).pointee.add(1, ordering: .relaxed)
        return noErr
    }
    let fw = counter(ctx, .framesWritten).pointee.load(ordering: .relaxed)
    let fr = counter(ctx, .framesRead).pointee.load(ordering: .acquiring)
    let bw = counter(ctx, .blocksWritten).pointee.load(ordering: .relaxed)
    let br = counter(ctx, .blocksRead).pointee.load(ordering: .acquiring)
    if fw &- fr &+ UInt64(frames) > ctx.pointee.frameCapacity || bw &- br >= ctx.pointee.recordCapacity {
        counter(ctx, .droppedFrames).pointee.add(UInt64(frames), ordering: .relaxed)
        counter(ctx, .droppedBlocks).pointee.add(1, ordering: .relaxed)
        return noErr
    }
    let mask = ctx.pointee.frameCapacity &- 1
    let samples = ctx.pointee.samples
    c = 0
    while c < channels {
        let s = ctx.pointee.sources[c]
        let base = abl[Int(s.buffer)].mData.unsafelyUnwrapped.assumingMemoryBound(to: Float.self)
        let stride = Int(s.stride), offset = Int(s.offset)
        var f = 0
        while f < frames {
            let slot = Int((fw &+ UInt64(f)) & mask)
            samples[slot &* channels &+ c] = base[f &* stride &+ offset]
            f += 1
        }
        c += 1
    }
    let ts = inInputTime.pointee
    let exit = mach_absolute_time()
    ctx.pointee.records[Int(bw & (ctx.pointee.recordCapacity &- 1))] = BlockRecord(
        sampleTime: ts.mSampleTime, hostTime: ts.mHostTime, flags: ts.mFlags.rawValue, frames: UInt32(frames),
        ringStart: fw, cbStart: entry, cbEnd: exit,
        droppedFrames: counter(ctx, .droppedFrames).pointee.load(ordering: .relaxed),
        droppedBlocks: counter(ctx, .droppedBlocks).pointee.load(ordering: .relaxed))
    counter(ctx, .framesWritten).pointee.store(fw &+ UInt64(frames), ordering: .releasing)
    counter(ctx, .blocksWritten).pointee.store(bw &+ 1, ordering: .releasing)
    counter(ctx, .callbackMaxTicks).pointee.max(exit &- entry, ordering: .relaxed)
    return noErr
}
// END REALTIME

let flags: UnsafeMutablePointer<Atomic<UInt64>> = {
    let p = UnsafeMutablePointer<Atomic<UInt64>>.allocate(capacity: 2)
    p.initialize(to: Atomic<UInt64>(0))
    (p + 1).initialize(to: Atomic<UInt64>(0))
    return p
}()
let stopFlag = flags, listenerMask = flags + 1

// BEGIN SELFTEST
func selfTest(_ args: ArraySlice<String>) -> Int32 {
    guard let o = options(args, ["blocks", "block-frames", "stall-after-blocks", "stall-ms", "ring-frames"]),
          let blocks = integer(o, "blocks", 400, 1...100_000),
          let blockFrames = integer(o, "block-frames", 480, 1...8192),
          let stallAfter = integer(o, "stall-after-blocks", 20, 0...100_000),
          let stallMs = integer(o, "stall-ms", 300, 0...30_000),
          let ringFrames = integer(o, "ring-frames", 4096, 1...(1 << 24)) else {
        return usage("self-test takes --blocks --block-frames --stall-after-blocks --stall-ms --ring-frames")
    }
    let channels = 2
    let ctx = makeContext(sources: [ChannelSource(buffer: 0, offset: 0, stride: 2),
                                    ChannelSource(buffer: 0, offset: 1, stride: 2)],
                          bufferChannels: [2], frames: UInt64(ringFrames), maxBlockFrames: 8192)
    let wire = Wire()
    let device: [String: Any] = ["uid": "self-test", "name": "self-test", "input_channels": channels,
                                 "nominal_sample_rate": 48000, "alive": true, "hog_mode_pid": -1,
                                 "stream_configuration": [channels]]
    _ = wire.json(frameHello, helloSchema, ["mode": "self-test", "helper_pid": Int(getpid()),
                                            "macos_version": macOSVersion(), "device": device,
                                            "microphone_authorization": "not_applicable", "timebase": timebase(),
                                            "host_time_scope": "mach_absolute_time", "max_block_frames": 8192,
                                            "refusal": [String]()])
    _ = wire.json(frameStarted, startedSchema, ["selected_channels": [0, 1],
                                                "ring_frames": Int(ctx.pointee.frameCapacity),
                                                "ring_bytes": Int(ctx.pointee.frameCapacity) * channels * 4,
                                                "max_block_frames": 8192, "host_now_ticks": mach_absolute_time()])
    let done = UnsafeMutablePointer<Atomic<Bool>>.allocate(capacity: 1)
    done.initialize(to: Atomic<Bool>(false))
    let producer = Thread {
        let abl = AudioBufferList.allocate(maximumBuffers: 1)
        let data = UnsafeMutablePointer<Float>.allocate(capacity: blockFrames * channels)
        abl[0] = AudioBuffer(mNumberChannels: UInt32(channels), mDataByteSize: UInt32(blockFrames * channels * 4),
                             mData: UnsafeMutableRawPointer(data))
        let out = AudioBufferList.allocate(maximumBuffers: 1)
        var ts = AudioTimeStamp()
        ts.mFlags = [.sampleTimeValid, .hostTimeValid]
        let unused = AudioTimeStamp()
        for b in 0..<blocks {
            for f in 0..<blockFrames {
                data[f * 2] = Float(b * blockFrames + f) / 1e7
                data[f * 2 + 1] = -data[f * 2]
            }
            ts.mSampleTime = Double(b * blockFrames)
            ts.mHostTime = mach_absolute_time()
            withUnsafePointer(to: ts) { input in
                withUnsafePointer(to: unused) { other in
                    _ = listenIOProc(0, other, UnsafePointer(abl.unsafePointer), input, out.unsafeMutablePointer,
                                     other, UnsafeMutableRawPointer(ctx))
                }
            }
            usleep(1000)
        }
        done.pointee.store(true, ordering: .releasing)
    }
    producer.start()
    var drained = 0, stalled = false
    var lastBeat = DispatchTime.now().uptimeNanoseconds
    while true {
        let finished = done.pointee.load(ordering: .acquiring)
        if !stalled && drained >= stallAfter {
            stalled = true
            usleep(UInt32(stallMs) * 1000)
        }
        let n = drainOnce(ctx, wire)
        if n < 0 { return 5 }
        drained += n
        if elapsedMs(since: lastBeat) >= 250 {
            if !heartbeat(ctx, wire) { return 5 }
            lastBeat = DispatchTime.now().uptimeNanoseconds
        }
        if finished && n == 0 && value(ctx, .blocksRead) == value(ctx, .blocksWritten) { break }
        usleep(2000)
    }
    let produced = blocks * blockFrames
    _ = wire.json(frameExit, exitSchema, counters(ctx).merging(["reason": "self_test_complete",
                                                                "produced_frames": produced,
                                                                "produced_blocks": blocks]) { _, new in new })
    return wire.failed ? 5 : 0
}
// END SELFTEST

func address(_ selector: AudioObjectPropertySelector, _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal,
             _ element: AudioObjectPropertyElement = kAudioObjectPropertyElementMain) -> AudioObjectPropertyAddress {
    AudioObjectPropertyAddress(mSelector: selector, mScope: scope, mElement: element)
}

func readValue<T>(_ id: AudioObjectID, _ addr: AudioObjectPropertyAddress, _ initial: T) -> T? {
    var a = addr
    var result = initial
    var size = UInt32(MemoryLayout<T>.size)
    let status = withUnsafeMutablePointer(to: &result) { AudioObjectGetPropertyData(id, &a, 0, nil, &size, $0) }
    return status == noErr && size == UInt32(MemoryLayout<T>.size) ? result : nil
}

func readString(_ id: AudioObjectID, _ addr: AudioObjectPropertyAddress) -> String? {
    var a = addr
    var result: Unmanaged<CFString>? = nil
    var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    let status = withUnsafeMutablePointer(to: &result) { AudioObjectGetPropertyData(id, &a, 0, nil, &size, $0) }
    guard status == noErr, let string = result else { return nil }
    return string.takeRetainedValue() as String      // CoreAudio returns a +1 CFString
}

func readArray<T>(_ id: AudioObjectID, _ addr: AudioObjectPropertyAddress, _ zero: T) -> [T] {
    readArrayChecked(id, addr, zero) ?? []
}

func readArrayChecked<T>(_ id: AudioObjectID, _ addr: AudioObjectPropertyAddress, _ zero: T) -> [T]? {
    var a = addr
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(id, &a, 0, nil, &size) == noErr else { return nil }
    guard size > 0 else { return [] }
    var items = [T](repeating: zero, count: Int(size) / MemoryLayout<T>.stride)
    let status = items.withUnsafeMutableBytes { AudioObjectGetPropertyData(id, &a, 0, nil, &size, $0.baseAddress!) }
    return status == noErr ? Array(items.prefix(Int(size) / MemoryLayout<T>.stride)) : nil
}

func inputBufferChannels(_ id: AudioObjectID) -> [UInt32] { readBufferChannels(id) ?? [] }

func readBufferChannels(_ id: AudioObjectID) -> [UInt32]? {
    var a = address(kAudioDevicePropertyStreamConfiguration, kAudioObjectPropertyScopeInput)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(id, &a, 0, nil, &size) == noErr else { return nil }
    guard Int(size) >= MemoryLayout<AudioBufferList>.size else { return [] }
    let raw = UnsafeMutableRawPointer.allocate(byteCount: Int(size), alignment: MemoryLayout<AudioBufferList>.alignment)
    defer { raw.deallocate() }
    guard AudioObjectGetPropertyData(id, &a, 0, nil, &size, raw) == noErr else { return nil }
    return UnsafeMutableAudioBufferListPointer(raw.assumingMemoryBound(to: AudioBufferList.self)).map { $0.mNumberChannels }
}

func fourCC(_ v: UInt32) -> String {
    let bytes = [UInt8((v >> 24) & 0xff), UInt8((v >> 16) & 0xff), UInt8((v >> 8) & 0xff), UInt8(v & 0xff)]
    return bytes.allSatisfy({ $0 >= 0x20 && $0 < 0x7f }) ? String(decoding: bytes, as: UTF8.self)
        : String(format: "0x%08x", v)
}

func transport(_ v: UInt32) -> String {
    switch v {
    case kAudioDeviceTransportTypeBuiltIn: return "builtin"
    case kAudioDeviceTransportTypeUSB: return "usb"
    case kAudioDeviceTransportTypeVirtual: return "virtual"
    case kAudioDeviceTransportTypeAggregate, kAudioDeviceTransportTypeAutoAggregate: return "aggregate"
    case kAudioDeviceTransportTypeBluetooth, kAudioDeviceTransportTypeBluetoothLE: return "bluetooth"
    case kAudioDeviceTransportTypeHDMI: return "hdmi"
    case kAudioDeviceTransportTypeDisplayPort: return "displayport"
    case kAudioDeviceTransportTypeAirPlay: return "airplay"
    case kAudioDeviceTransportTypeAVB: return "avb"
    case kAudioDeviceTransportTypeThunderbolt: return "thunderbolt"
    case kAudioDeviceTransportTypePCI: return "pci"
    case kAudioDeviceTransportTypeFireWire: return "firewire"
    default: return "other"
    }
}

func json(_ v: Int?) -> Any { v.map { $0 as Any } ?? NSNull() }

func number(_ d: Double) -> Any {
    guard d.isFinite else { return NSNull() }
    return d == d.rounded() && abs(d) < 9e15 ? Int(d) : d
}

struct StreamInfo {
    var id: AudioStreamID
    var startingChannel: Int?      // 0-based; CoreAudio's StartingChannel element is 1-based
    var format: AudioStreamBasicDescription?
    var channels: Int { Int(format?.mChannelsPerFrame ?? 0) }
}

struct DeviceInfo {
    var id: AudioObjectID
    var uid: String
    var name: String
    var manufacturer: String?
    var modelUID: String?
    var transport: String
    var alive: Bool
    var hogPID: Int32?
    var nominalRate: Double
    var bufferChannels: [UInt32]
    var streams: [StreamInfo]
    var channelNames: [String]
    var bufferFrameSize: UInt32?
    var latency: UInt32?
    var safetyOffset: UInt32?
    var inputChannels: Int { bufferChannels.reduce(0) { $0 + Int($1) } }
}

func readStreams(_ id: AudioObjectID) -> [StreamInfo] {
    readArray(id, address(kAudioDevicePropertyStreams, kAudioObjectPropertyScopeInput), AudioStreamID(0)).map { s in
        let start = readValue(s, address(kAudioStreamPropertyStartingChannel), UInt32(0)).flatMap { $0 > 0 ? Int($0) - 1 : nil }
        return StreamInfo(id: s, startingChannel: start,
                          format: readValue(s, address(kAudioStreamPropertyVirtualFormat), AudioStreamBasicDescription()))
    }
}

func readStreamsChecked(_ id: AudioObjectID) -> [StreamInfo]? {
    guard let ids = readArrayChecked(id, address(kAudioDevicePropertyStreams, kAudioObjectPropertyScopeInput),
                                     AudioStreamID(0)) else { return nil }
    var streams: [StreamInfo] = []
    for s in ids {
        guard let first = readValue(s, address(kAudioStreamPropertyStartingChannel), UInt32(0)),
              let format = readValue(s, address(kAudioStreamPropertyVirtualFormat), AudioStreamBasicDescription())
        else { return nil }
        streams.append(StreamInfo(id: s, startingChannel: first > 0 ? Int(first) - 1 : nil, format: format))
    }
    return streams
}

func readDevice(_ id: AudioObjectID) -> DeviceInfo {
    let buffers = inputBufferChannels(id)
    let channels = buffers.reduce(0) { $0 + Int($1) }
    let names = (0..<min(channels, 64)).map { ch in
        readString(id, address(kAudioObjectPropertyElementName, kAudioObjectPropertyScopeInput, UInt32(ch + 1))) ?? ""
    }
    return DeviceInfo(
        id: id, uid: readString(id, address(kAudioDevicePropertyDeviceUID)) ?? "",
        name: readString(id, address(kAudioObjectPropertyName)) ?? "",
        manufacturer: readString(id, address(kAudioObjectPropertyManufacturer)),
        modelUID: readString(id, address(kAudioDevicePropertyModelUID)),
        transport: transport(readValue(id, address(kAudioDevicePropertyTransportType), UInt32(0)) ?? 0),
        alive: (readValue(id, address(kAudioDevicePropertyDeviceIsAlive), UInt32(0)) ?? 0) != 0,
        hogPID: readValue(id, address(kAudioDevicePropertyHogMode), pid_t(-1)),
        nominalRate: readValue(id, address(kAudioDevicePropertyNominalSampleRate), Float64(0)) ?? 0,
        bufferChannels: buffers, streams: readStreams(id), channelNames: names,
        bufferFrameSize: readValue(id, address(kAudioDevicePropertyBufferFrameSize), UInt32(0)),
        latency: readValue(id, address(kAudioDevicePropertyLatency, kAudioObjectPropertyScopeInput), UInt32(0)),
        safetyOffset: readValue(id, address(kAudioDevicePropertySafetyOffset, kAudioObjectPropertyScopeInput), UInt32(0)))
}

func formatJSON(_ f: AudioStreamBasicDescription?) -> Any {
    guard let f = f else { return NSNull() }
    return ["format_id": fourCC(f.mFormatID), "float": f.mFormatFlags & kAudioFormatFlagIsFloat != 0,
            "bits_per_channel": Int(f.mBitsPerChannel), "channels_per_frame": Int(f.mChannelsPerFrame),
            "interleaved": f.mFormatFlags & kAudioFormatFlagIsNonInterleaved == 0,
            "packed": f.mFormatFlags & kAudioFormatFlagIsPacked != 0,
            "native_endian": f.mFormatFlags & kAudioFormatFlagIsBigEndian == 0, "sample_rate": number(f.mSampleRate)]
}

func deviceJSON(_ d: DeviceInfo) -> [String: Any] {
    func optional(_ v: UInt32?) -> Any { v.map { Int($0) } ?? NSNull() }
    return ["uid": d.uid, "name": d.name, "manufacturer": d.manufacturer ?? NSNull(), "model_uid": d.modelUID ?? NSNull(),
            "transport": d.transport, "audio_object_id": Int(d.id), "alive": d.alive,
            "hog_mode_pid": json(d.hogPID.map { Int($0) }), "nominal_sample_rate": number(d.nominalRate),
            "input_channels": d.inputChannels,
            "stream_configuration": d.bufferChannels.map { Int($0) },
            "input_streams": d.streams.map { ["starting_channel": json($0.startingChannel), "channels": $0.channels,
                                              "virtual_format": formatJSON($0.format)] },
            "channel_names": d.channelNames, "buffer_frame_size": optional(d.bufferFrameSize),
            "device_latency_frames": optional(d.latency), "safety_offset_frames": optional(d.safetyOffset)]
}

func isFloat32(_ f: AudioStreamBasicDescription?, rate: Double) -> Bool {
    guard let f = f else { return false }
    return f.mFormatID == kAudioFormatLinearPCM && f.mFormatFlags & kAudioFormatFlagIsFloat != 0
        && f.mFormatFlags & kAudioFormatFlagIsPacked != 0 && f.mFormatFlags & kAudioFormatFlagIsBigEndian == 0
        && f.mBitsPerChannel == 32 && abs(f.mSampleRate - rate) < 1e-6
}

func sameFormat(_ a: AudioStreamBasicDescription?, _ b: AudioStreamBasicDescription?) -> Bool {
    guard let a = a, let b = b else { return a == nil && b == nil }
    return a.mSampleRate == b.mSampleRate && a.mFormatID == b.mFormatID && a.mFormatFlags == b.mFormatFlags
        && a.mBytesPerPacket == b.mBytesPerPacket && a.mFramesPerPacket == b.mFramesPerPacket
        && a.mBytesPerFrame == b.mBytesPerFrame && a.mChannelsPerFrame == b.mChannelsPerFrame
        && a.mBitsPerChannel == b.mBitsPerChannel
}

func microphoneAuthorization() -> String {
    switch AVCaptureDevice.authorizationStatus(for: .audio) {
    case .authorized: return "authorized"
    case .denied: return "denied"
    case .restricted: return "restricted"
    case .notDetermined: return "not_determined"
    @unknown default: return "unknown"
    }
}

let systemObject = AudioObjectID(kAudioObjectSystemObject)

func inputDevices() -> ([AudioObjectID], Int) {
    let configured = readArray(systemObject, address(kAudioHardwarePropertyDevices), AudioObjectID(0)).map {
        ($0, readBufferChannels($0))
    }
    let inputs = configured.filter { $0.1.map { !$0.allSatisfy { $0 == 0 } } ?? false }.map { $0.0 }
    return (inputs, configured.filter { $0.1 == nil }.count)
}

func translateUID(_ uid: String) -> AudioObjectID? {
    var a = address(kAudioHardwarePropertyTranslateUIDToDevice)
    var qualifier = uid as CFString
    var device = AudioObjectID(kAudioObjectUnknown)
    var size = UInt32(MemoryLayout<AudioObjectID>.size)
    let status = withUnsafeMutablePointer(to: &qualifier) { q in
        AudioObjectGetPropertyData(systemObject, &a, UInt32(MemoryLayout<CFString>.size), q, &size, &device)
    }
    guard status == noErr, device != kAudioObjectUnknown,
          readString(device, address(kAudioDevicePropertyDeviceUID)) == uid else { return nil }
    return device
}

func listDevices() -> Int32 {
    let (devices, unreadable) = inputDevices()
    let document: [String: Any] = [
        "schema": "pocket.listen-helper-devices/v1", "macos_version": macOSVersion(),
        "microphone_authorization": microphoneAuthorization(), "timebase": timebase(),
        "devices": devices.prefix(64).map { deviceJSON(readDevice($0)) }, "devices_omitted": max(devices.count - 64, 0),
        "devices_unreadable": unreadable]
    guard JSONSerialization.isValidJSONObject(document),
          let data = try? JSONSerialization.data(withJSONObject: document, options: [.sortedKeys]) else { return 5 }
    return writeAll([UInt8](data) + [10]) ? 0 : 5
}

enum Command { case line(String), eof, none }

final class Commands {
    private let lock = NSLock()
    private let ready = DispatchSemaphore(value: 0)
    private var lines: [String] = []
    private var eof = false

    func start() {
        Thread { [self] in
            var line: [UInt8] = [], chunk = [UInt8](repeating: 0, count: 512)
            while true {
                let n = Darwin.read(0, &chunk, chunk.count)
                if n < 0 && errno == EINTR { continue }
                if n <= 0 { finish(); return }
                for byte in chunk[0..<n] {
                    if byte == 10 {
                        push(String(decoding: line, as: UTF8.self))
                        line.removeAll(keepingCapacity: true)
                    } else if line.count < 256 {
                        line.append(byte)
                    } else {
                        push("\u{0}overlong")
                        line.removeAll(keepingCapacity: true)
                    }
                }
            }
        }.start()
    }

    private func push(_ text: String) { lock.lock(); lines.append(text); lock.unlock(); ready.signal() }
    private func finish() { lock.lock(); eof = true; lock.unlock(); ready.signal() }

    func next(waitMs: Int) -> Command {
        if let command = take() { return command }
        if waitMs > 0 { _ = ready.wait(timeout: .now() + .milliseconds(waitMs)) }
        return take() ?? .none
    }

    private func take() -> Command? {
        lock.lock()
        defer { lock.unlock() }
        if !lines.isEmpty { return .line(lines.removeFirst()) }
        return eof ? .eof : nil
    }
}

func parseStart(_ line: String, inputChannels: Int) -> [Int]? {
    guard line.hasPrefix("start ") else { return nil }
    let parts = line.dropFirst(6).split(separator: ",", omittingEmptySubsequences: false).map { Int($0) }
    guard (1...4).contains(parts.count), parts.allSatisfy({ $0 != nil }) else { return nil }
    let channels = parts.map { $0! }
    guard Set(channels).count == channels.count, channels.allSatisfy({ $0 >= 0 && $0 < inputChannels }) else { return nil }
    return channels
}

func refusals(_ d: DeviceInfo?, expectedRate: Int, authorization: String) -> [String] {
    var reasons: [String] = []
    guard let d = d else { return ["device_not_found"] + (authorization == "authorized" ? [] : ["microphone_not_authorized"]) }
    if !d.alive { reasons.append("device_not_alive") }
    if let hog = d.hogPID, hog != -1 && hog != getpid() { reasons.append("device_hogged") }
    if authorization != "authorized" { reasons.append("microphone_not_authorized") }
    if abs(d.nominalRate - Double(expectedRate)) > 1e-6 { reasons.append("sample_rate_mismatch") }
    return reasons
}

let watchedBits: [(AudioObjectPropertySelector, AudioObjectPropertyScope, UInt64, String)] = [
    (kAudioDevicePropertyDeviceIsAlive, kAudioObjectPropertyScopeGlobal, 1 << 0, "device_is_alive"),
    (kAudioDevicePropertyNominalSampleRate, kAudioObjectPropertyScopeGlobal, 1 << 1, "nominal_sample_rate"),
    (kAudioDevicePropertyStreamConfiguration, kAudioObjectPropertyScopeInput, 1 << 2, "stream_configuration"),
    (kAudioDevicePropertyHogMode, kAudioObjectPropertyScopeGlobal, 1 << 3, "hog_mode"),
    (kAudioDeviceProcessorOverload, kAudioObjectPropertyScopeGlobal, 1 << 4, "processor_overload"),
    (kAudioDevicePropertyStreams, kAudioObjectPropertyScopeInput, 1 << 2, "streams")]
let formatBit: UInt64 = 1 << 5, devicesBit: UInt64 = 1 << 6, overloadBit: UInt64 = 1 << 4

struct Listener {
    var object: AudioObjectID
    var address: AudioObjectPropertyAddress
    var block: AudioObjectPropertyListenerBlock
}

func listen(_ object: AudioObjectID, _ addr: AudioObjectPropertyAddress, _ bit: UInt64,
            _ queue: DispatchQueue) -> Listener? {
    let block: AudioObjectPropertyListenerBlock = { _, _ in listenerMask.pointee.bitwiseOr(bit, ordering: .relaxed) }
    var a = addr
    return AudioObjectAddPropertyListenerBlock(object, &a, queue, block) == noErr
        ? Listener(object: object, address: addr, block: block) : nil
}

// Rate first: a rate change also changes stream formats.
func deviceChange(_ start: DeviceInfo, expectedRate: Int) -> (String, [String: Any])? {
    guard let alive = readValue(start.id, address(kAudioDevicePropertyDeviceIsAlive), UInt32(0)) else {
        return ("device_property_unreadable", ["property": "device_is_alive"])
    }
    if alive == 0 { return ("device_lost", ["device_is_alive": false]) }
    guard let rate = readValue(start.id, address(kAudioDevicePropertyNominalSampleRate), Float64(0)) else {
        return ("device_property_unreadable", ["property": "nominal_sample_rate"])
    }
    if abs(rate - Double(expectedRate)) > 1e-6 { return ("sample_rate_changed", ["nominal_sample_rate": number(rate)]) }
    guard let hog = readValue(start.id, address(kAudioDevicePropertyHogMode), pid_t(-1)) else {
        return ("device_property_unreadable", ["property": "hog_mode"])
    }
    if hog != -1 && hog != getpid() { return ("device_hogged", ["hog_mode_pid": Int(hog)]) }
    guard let buffers = readBufferChannels(start.id) else {
        return ("device_property_unreadable", ["property": "stream_configuration"])
    }
    guard let streams = readStreamsChecked(start.id) else {
        return ("device_property_unreadable", ["property": "input_streams"])
    }
    if buffers != start.bufferChannels || streams.map({ $0.id }) != start.streams.map({ $0.id })
        || zip(streams, start.streams).contains(where: { $0.startingChannel != $1.startingChannel }) {
        return ("channel_map_changed", ["stream_configuration": buffers.map { Int($0) }])
    }
    if zip(streams, start.streams).contains(where: { !sameFormat($0.format, $1.format) }) {
        return ("format_changed", ["input_streams": streams.map { ["starting_channel": json($0.startingChannel),
                                                                    "virtual_format": formatJSON($0.format)] }])
    }
    return nil
}

func capture(_ args: ArraySlice<String>) -> Int32 {
    guard let o = options(args, ["device-uid", "expected-sample-rate", "ring-seconds", "max-block-frames"]),
          let uid = o["device-uid"], !uid.isEmpty, uid.utf8.count <= 256,
          let rate = integer(o, "expected-sample-rate", nil, 8000...384_000),
          let ringSeconds = integer(o, "ring-seconds", 2, 1...10),
          let maxBlock = integer(o, "max-block-frames", 8192, 32...8192) else {
        return usage("capture takes --device-uid U --expected-sample-rate R [--ring-seconds S] [--max-block-frames M]")
    }
    signal(SIGINT, SIG_IGN)
    signal(SIGTERM, SIG_IGN)
    let control = DispatchQueue(label: "pocket.listen.control")
    let terminate = DispatchSource.makeSignalSource(signal: SIGTERM, queue: control)
    terminate.setEventHandler { stopFlag.pointee.store(1, ordering: .relaxed) }
    terminate.resume()
    let commands = Commands()
    commands.start()
    let wire = Wire()

    let authorization = microphoneAuthorization()
    let device = translateUID(uid).map(readDevice)
    let refused = refusals(device, expectedRate: rate, authorization: authorization)
    var hello: [String: Any] = ["mode": "capture", "helper_pid": Int(getpid()), "macos_version": macOSVersion(),
                                "device": device.map(deviceJSON) ?? NSNull(), "microphone_authorization": authorization,
                                "timebase": timebase(), "host_time_scope": "mach_absolute_time",
                                "max_block_frames": maxBlock, "refusal": refused]
    if device == nil { hello["requested_uid"] = uid }
    _ = wire.json(frameHello, helloSchema, hello)
    if !refused.isEmpty {
        _ = wire.json(frameExit, exitSchema, ["reason": "refused", "refusal": refused])
        return 3
    }
    let info = device!

    let waitStarted = DispatchTime.now().uptimeNanoseconds
    var selected: [Int]? = nil
    while selected == nil {
        if stopFlag.pointee.load(ordering: .relaxed) != 0 {
            _ = wire.json(frameExit, exitSchema, ["reason": "terminated"]); return 0
        }
        if elapsedMs(since: waitStarted) >= 10_000 {
            _ = wire.json(frameExit, exitSchema, ["reason": "start_timeout"]); return 0
        }
        switch commands.next(waitMs: 50) {
        case .none: continue
        case .eof: _ = wire.json(frameExit, exitSchema, ["reason": "stdin_closed"]); return 0
        case .line("quit"): _ = wire.json(frameExit, exitSchema, ["reason": "quit"]); return 0
        case .line(let line):
            guard let channels = parseStart(line, inputChannels: info.inputChannels) else {
                _ = wire.json(frameExit, exitSchema, ["reason": "invalid_command"]); return 2
            }
            selected = channels
        }
    }
    let channels = selected!

    var bufferStart = 0, sources: [ChannelSource] = []
    var bufferOf: [Int: (Int, Int, Int)] = [:]
    for (b, n) in info.bufferChannels.enumerated() {
        for ch in bufferStart..<(bufferStart + Int(n)) { bufferOf[ch] = (b, ch - bufferStart, Int(n)) }
        bufferStart += Int(n)
    }
    for ch in channels {
        let owners = info.streams.filter { s in s.startingChannel.map { $0 <= ch && ch < $0 + s.channels } ?? false }
        guard owners.count == 1, isFloat32(owners[0].format, rate: info.nominalRate), let place = bufferOf[ch] else {
            _ = wire.json(frameExit, exitSchema, ["reason": "refused", "refusal": ["format_not_float32"]]); return 3
        }
        sources.append(ChannelSource(buffer: Int32(place.0), offset: Int32(place.1), stride: Int32(place.2)))
    }
    let ctx = makeContext(sources: sources, bufferChannels: info.bufferChannels,
                          frames: UInt64(ringSeconds) * UInt64(rate), maxBlockFrames: UInt32(maxBlock))

    let queue = DispatchQueue(label: "pocket.listen.properties")
    var listeners: [Listener] = [], unwatched: [String] = []
    for (selector, scope, bit, name) in watchedBits {
        if let l = listen(info.id, address(selector, scope), bit, queue) { listeners.append(l) } else { unwatched.append(name) }
    }
    for stream in info.streams {
        if let l = listen(stream.id, address(kAudioStreamPropertyVirtualFormat), formatBit, queue) {
            listeners.append(l)
        } else { unwatched.append("virtual_format") }
    }
    if let l = listen(systemObject, address(kAudioHardwarePropertyDevices), devicesBit, queue) {
        listeners.append(l)
    } else { unwatched.append("devices") }
    func removeListeners() {
        for l in listeners {
            var a = l.address
            _ = AudioObjectRemovePropertyListenerBlock(l.object, &a, queue, l.block)
        }
    }

    let lateAuthorization = microphoneAuthorization()
    let change = deviceChange(info, expectedRate: rate)
    let late = change?.0 == "device_property_unreadable" ? []
        : refusals(readDevice(info.id), expectedRate: rate, authorization: lateAuthorization)
    if !late.isEmpty {
        removeListeners()
        _ = wire.json(frameExit, exitSchema, ["reason": "refused", "refusal": late,
                                              "microphone_authorization": lateAuthorization]); return 3
    }
    if let (type, detail) = change {
        removeListeners()
        _ = wire.json(frameExit, exitSchema, ["reason": "device_changed_before_start", "type": type, "value": detail])
        return 4
    }
    var procID: AudioDeviceIOProcID? = nil
    let created = AudioDeviceCreateIOProcID(info.id, listenIOProc, UnsafeMutableRawPointer(ctx), &procID)
    guard created == noErr, let proc = procID else {
        removeListeners()
        _ = wire.json(frameExit, exitSchema, ["reason": "start_failed", "stage": "create_ioproc", "osstatus": Int(created)])
        return 5
    }
    let started = AudioDeviceStart(info.id, proc)
    if started != noErr {
        _ = AudioDeviceDestroyIOProcID(info.id, proc)
        removeListeners()
        _ = wire.json(frameExit, exitSchema, ["reason": "start_failed", "stage": "start", "osstatus": Int(started)])
        return 5
    }
    _ = wire.json(frameStarted, startedSchema, ["selected_channels": channels, "ring_frames": Int(ctx.pointee.frameCapacity),
                                                "ring_bytes": Int(ctx.pointee.frameCapacity) * channels.count * 4,
                                                "record_capacity": Int(recordCapacity), "max_block_frames": maxBlock,
                                                "host_now_ticks": mach_absolute_time(), "unwatched_properties": unwatched])

    var reason = "quit", code: Int32 = 0, deliverRest = true
    var lastBeat = DispatchTime.now().uptimeNanoseconds, lastPoll = lastBeat
    var mismatches: UInt64 = 0
    loop: while true {
        if stopFlag.pointee.load(ordering: .relaxed) != 0 { reason = "terminated"; break loop }
        switch commands.next(waitMs: 0) {
        case .none: break
        case .eof: reason = "stdin_closed"; break loop
        case .line("quit"): reason = "quit"; break loop
        case .line: reason = "invalid_command"; code = 2; break loop
        }
        let bits = listenerMask.pointee.exchange(0, ordering: .relaxed)
        if bits & overloadBit != 0 {
            _ = wire.json(frameEvent, eventSchema, ["type": "processor_overload", "value": NSNull(),
                                                    "host_now_ticks": mach_absolute_time()])
        }
        if bits & devicesBit != 0 {
            if translateUID(uid) != info.id {
                _ = wire.json(frameEvent, eventSchema, ["type": "device_lost", "value": ["uid_resolves": false],
                                                        "host_now_ticks": mach_absolute_time()])
                reason = "device_event"; code = 4; deliverRest = false; break loop
            }
            _ = wire.json(frameEvent, eventSchema, ["type": "devices_changed", "value": NSNull(),
                                                    "host_now_ticks": mach_absolute_time()])
        }
        let layout = value(ctx, .layoutMismatchBlocks)
        if layout != mismatches {
            mismatches = layout
            _ = wire.json(frameEvent, eventSchema, ["type": "channel_map_changed",
                                                    "value": ["layout_mismatch_blocks": layout],
                                                    "host_now_ticks": mach_absolute_time()])
            reason = "device_event"; code = 4; deliverRest = false; break loop
        }
        if bits & ~(overloadBit | devicesBit) != 0 || elapsedMs(since: lastPoll) >= 250 {
            lastPoll = DispatchTime.now().uptimeNanoseconds
            if let (type, detail) = deviceChange(info, expectedRate: rate) {
                _ = wire.json(frameEvent, eventSchema, ["type": type, "value": detail,
                                                        "host_now_ticks": mach_absolute_time()])
                reason = "device_event"; code = 4; deliverRest = false; break loop
            }
        }
        let drained = drainOnce(ctx, wire)
        if drained < 0 { reason = "stdout_closed"; code = 5; deliverRest = false; break loop }
        if elapsedMs(since: lastBeat) >= 250 {
            if !heartbeat(ctx, wire) { reason = "stdout_closed"; code = 5; deliverRest = false; break loop }
            lastBeat = DispatchTime.now().uptimeNanoseconds
        }
        if drained == 0 { usleep(2000) }
    }
    _ = AudioDeviceStop(info.id, proc)
    _ = AudioDeviceDestroyIOProcID(info.id, proc)
    removeListeners()
    if deliverRest { while drainOnce(ctx, wire) > 0 {} }
    let undelivered = value(ctx, .blocksWritten) &- value(ctx, .blocksRead)
    let undeliveredFrames = value(ctx, .framesWritten) &- value(ctx, .framesRead)
    _ = wire.json(frameExit, exitSchema, counters(ctx).merging(["reason": reason, "undelivered_blocks": undelivered,
                                                               "undelivered_frames": undeliveredFrames]) { _, new in new })
    return wire.failed && code == 0 ? 5 : code
}

signal(SIGPIPE, SIG_IGN)
let arguments = CommandLine.arguments.dropFirst()
switch arguments.first {
case "self-test": exit(selfTest(arguments.dropFirst()))
case "list-devices": exit(arguments.count == 1 ? listDevices() : usage("list-devices takes no arguments"))
case "capture": exit(capture(arguments.dropFirst()))
default: exit(usage("modes: list-devices | capture --device-uid U --expected-sample-rate R | self-test"))
}
