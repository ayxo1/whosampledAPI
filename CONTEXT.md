# Music Sampling Lookup

This context describes recorded-music sampling relationships returned by the local API.

## Language

**Recording**:
A particular released performance of a piece of music. Alternate recordings, live versions, remasters, and mixes remain distinct when the source treats them as distinct.
_Avoid_: Song, track

**Observation**:
A set of music-sampling facts reported by a source at a recorded point in time. An Observation supports later auditing without claiming that the facts form a permanent snapshot.
_Avoid_: Snapshot

**Samples**:
The public API collection of Sample Uses attributed to a requested artist and exposed across the source site's numbered artist Samples pages. Relationships that require per-recording expansion are outside this collection.
_Avoid_: Sample lookup, sample result

**Sample Use**:
A relationship in which a Sampling Recording incorporates recorded audio from Source Material. Recreated performances such as interpolations are not Sample Uses.
_Avoid_: Sample

**Sampled Element**:
A source-reported classification of the audio reused by a Sample Use, such as drums, vocals, or multiple elements. It describes the relationship as a whole rather than an individual position.
_Avoid_: Sample type, passage element

**Source Position**:
A source-reported start marker in the Source Material where audio used by a Sample Use appears. It does not imply an end time or duration.
_Avoid_: Source Span, sampled section

**Sampling Position**:
A source-reported start marker in the Sampling Recording where audio from a Sample Use appears. It does not imply an end time or duration.
_Avoid_: Sampling Span, sample placement

**Sampling Credit**:
The attribution that connects an artist to a Sample Use as a performer of the Sampling Recording or as a producer. A Sample Use may have several Sampling Credits.
_Avoid_: Artist ownership

**Sampling Recording**:
The recording that incorporates material from another recording.
_Avoid_: Sampling song, destination track

**Source Material**:
The audio-bearing work from which a Sample Use takes recorded audio. Source Material may be a Source Recording, film, television program, or another kind of media.
_Avoid_: Sampled song, original track

**Source Recording**:
A music Recording that is the Source Material for a Sample Use.
_Avoid_: Sampled song, original track, Source Material
