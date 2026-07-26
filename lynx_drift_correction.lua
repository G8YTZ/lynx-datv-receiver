-- lynx_drift_correction.lua
--
-- Corrects for drift-from-live building up in a continuous, live UDP
-- transport stream. Deliberately does NOT watch mpv's own internal
-- buffer size (demuxer-cache-duration) - that's capped small by
-- --demuxer-max-bytes and confirmed live to stay flat (0.0-0.4s) even
-- during an incident where the actual delay-from-live grew far
-- larger, because the real drift was accumulating somewhere upstream
-- of mpv entirely (the network path or the combiner's own queue).
-- Watching mpv's own buffer size is structurally blind to that.
--
-- Instead: compare mpv's own playback position against genuine
-- wall-clock time directly. If playback advances by less than real
-- time actually elapsed, that shortfall IS the drift, regardless of
-- where in the pipeline it came from.
--
-- Response is tiered:
--   1. Small, accumulated drift -> nudge speed up slightly (3%). DISABLED
--      as of this version - confirmed live that mpv's speed property
--      combined with live audio, even without pitch correction, causes
--      genuine, cascading problems (audible sample-dropping stutter,
--      worsening over repeated cycles into full freezes). See
--      NUDGE_ENABLED below for the full reasoning.
--   2. Drift too large to tolerate -> mpv's own "drop-buffers" command
--      for an immediate resync. Visible as a brief glitch, not a
--      freeze - still far less disruptive than a full process restart,
--      and involves no audio resampling at all, so isn't exposed to
--      the bug that disabled step 1. This is currently the sole
--      correction mechanism.
--
-- Algorithm validated in isolation (see the accompanying test) before
-- being written here; the mpv API calls themselves (get_property_number,
-- add_periodic_timer, the exact behavior of "drop-buffers") have NOT
-- been tested against a real, running mpv instance - this needs
-- careful, live verification before being trusted in production.

local mp = require 'mp'

local CHECK_INTERVAL = 1.0          -- seconds between measurements
local NUDGE_SPEED = 1.03            -- 3% faster - closes 1s of drift in ~33s of real time
local NORMAL_SPEED = 1.0
local NUDGE_ENABLED = false         -- Disabled - confirmed live under marginal-signal
                                      -- conditions that a non-1.0 speed combined with live
                                      -- audio causes genuine, cascading problems (audible
                                      -- sample-dropping stutter, worsening over repeated
                                      -- nudge cycles into full freezes). Consistent with a
                                      -- broader, independently-documented mpv weakness
                                      -- (audio/video tracking non-1.0 speed differently,
                                      -- mpv GitHub #8234) beyond just the scaletempo2 A/V
                                      -- desync bug already avoided via
                                      -- --audio-pitch-correction=no. The tiered design
                                      -- itself stays - drop-buffers below now handles all
                                      -- drift correction, since it involves no audio
                                      -- resampling at all and so isn't exposed to this
                                      -- class of bug. Re-enabling this needs a genuinely
                                      -- different audio-handling approach verified safe
                                      -- under repeated toggling first (e.g. --af=rubberband
                                      -- as a distinct, differently-implemented alternative,
                                      -- untested here), not just flipping this back on.
local NUDGE_THRESHOLD = 0.5         -- seconds of estimated drift before nudging (unused
                                      -- while NUDGE_ENABLED is false) - avoids reacting to
                                      -- ordinary measurement noise
local DROP_BUFFERS_THRESHOLD = 1.5  -- seconds of estimated drift before an immediate,
                                      -- visible resync. Lowered from 3.0 now that nudging
                                      -- is disabled and can no longer catch drift early -
                                      -- this is now the sole correction mechanism, so drift
                                      -- shouldn't be left to grow as large before acting.
local STATUS_PATH = "/tmp/lynx_mpv_drift.json"

local last_real_time = nil
local last_playback_time = nil
local estimated_drift = 0.0
local nudge_active = false
local drop_buffers_count = 0  -- cumulative for this mpv session - NOT reset by reset_state(),
                                -- so lynx_app.py can detect increments as discrete events

-- Circuit breaker: confirmed live as a real, necessary safeguard even
-- independent of nudging - a genuine, ongoing RF/demodulation problem
-- produced 13 drop-buffers calls in under a minute in one incident,
-- each one "very disruptive" per mpv's own docs, with no sign of the
-- underlying issue actually resolving. Repeatedly calling an
-- experimental, disruptive command without any brake risks
-- destabilizing mpv's own state rather than helping. If this trips,
-- drop-buffers is suppressed for a cooldown - with nudging disabled
-- there is nothing else to fall back to, so this simply waits - and
-- the external playback-delay/hard-freeze monitors, which have their
-- own, separate, already-proven circuit breakers, are left to take
-- over with a full restart if the problem doesn't clear on its own.
local DROP_BUFFERS_BREAKER_THRESHOLD = 5     -- this many drop-buffers calls...
local DROP_BUFFERS_BREAKER_WINDOW = 40.0     -- ...within this many seconds...
local DROP_BUFFERS_BREAKER_COOLDOWN = 20.0   -- ...trips this many seconds of suppression.
-- Loosened from 3/20s/60s - those values were calibrated for a specific runaway
-- oscillation between nudging and drop-buffers that can no longer happen now that
-- nudging is disabled entirely (see NUDGE_ENABLED above). With nudging gone, the
-- cooldown fallback is now "do nothing" rather than the old "keep nudging" - a real
-- regression - so: wider window avoids mistaking legitimate, recurring resyncs under
-- sustained marginal conditions for a genuine runaway; higher threshold gives more room
-- before suppressing; shorter cooldown limits how long anything relies on the slower,
-- more disruptive external playback-delay/hard-freeze restart as the only backstop.
-- Reasoned estimate, not yet proven against live data - worth revisiting based on how
-- often drop-buffers actually fires in practice.
local recent_drop_buffers_times = {}
local breaker_tripped_until = 0.0
local last_action_at = 0.0  -- updated whenever speed actually changes or drop-buffers fires -
                              -- lets lynx_app.py's own delay-based monitor give itself a brief
                              -- grace period, since a speed change is exactly the kind of event
                              -- that could cause a brief, misleading blip in mpv's own internal
                              -- playback/buffered gap reading, mistaken for a fresh problem

-- Hard freeze detection - distinct from gradual drift, and handled
-- completely differently: if playback-time advances by essentially
-- nothing for a couple of consecutive seconds despite real time
-- genuinely passing, that's an unambiguous signal nudging/drop-buffers
-- won't help (nothing to "catch up" if playback isn't moving, and a
-- stuck decoder needs a fresh instance, not a resync). Signals
-- lynx_app.py directly via the status file to go straight to a full
-- restart, skipping the entire tiered drift-correction sequence -
-- confirmed live to add real, unhelpful delay when the actual problem
-- is a hard freeze rather than gradual drift.
local HARD_FREEZE_ADVANCE_THRESHOLD = 0.2     -- playback must advance at least this much per
                                                -- real second to not count as "frozen"
local HARD_FREEZE_CONSECUTIVE_CHECKS = 2       -- this many consecutive near-zero-advance checks
                                                 -- (~2s of genuinely zero progress) confirms a
                                                 -- hard freeze, distinct from gradual drift
local hard_freeze_streak = 0
local hard_freeze_detected_at = 0.0  -- 0 means "not currently frozen"; timestamp of first
                                       -- confirmation once one has been detected

local function write_status()
    local f = io.open(STATUS_PATH, "w")
    if f then
        local speed = mp.get_property_number("speed", 1.0)
        local breaker_active = mp.get_time() < breaker_tripped_until
        f:write(string.format(
            '{"estimated_drift_secs": %.2f, "speed": %.3f, "nudge_active": %s, ' ..
            '"drop_buffers_count": %d, "breaker_active": %s, "last_action_at": %.3f, ' ..
            '"hard_freeze_detected_at": %.3f, "t": %.3f}',
            estimated_drift, speed, tostring(nudge_active), drop_buffers_count,
            tostring(breaker_active), last_action_at, hard_freeze_detected_at, mp.get_time()
        ))
        f:close()
    end
end

local function reset_state()
    estimated_drift = 0.0
    last_real_time = nil
    last_playback_time = nil
    nudge_active = false
    mp.set_property("speed", NORMAL_SPEED)
    last_action_at = mp.get_time()
    hard_freeze_streak = 0
    hard_freeze_detected_at = 0.0
end

local function check_drift()
    local now = mp.get_time()
    local playback = mp.get_property_number("playback-time")

    if playback == nil then
        -- Not actually playing yet (still buffering/starting, or
        -- between streams) - nothing meaningful to measure right now.
        last_real_time = nil
        last_playback_time = nil
        return
    end

    if last_real_time ~= nil then
        local real_delta = now - last_real_time
        local playback_delta = playback - last_playback_time

        -- Hard freeze check first, and separately from the drift
        -- accumulator below - a genuine freeze (essentially zero
        -- playback progress despite real time passing) is a
        -- fundamentally different problem from gradual drift, and
        -- gets a fundamentally different response: skip straight to
        -- signalling for a full restart rather than trying a nudge or
        -- drop-buffers, neither of which can help when playback isn't
        -- advancing at all.
        if playback_delta < HARD_FREEZE_ADVANCE_THRESHOLD then
            hard_freeze_streak = hard_freeze_streak + 1
        else
            hard_freeze_streak = 0
            hard_freeze_detected_at = 0.0
        end
        if hard_freeze_streak >= HARD_FREEZE_CONSECUTIVE_CHECKS and hard_freeze_detected_at == 0.0 then
            mp.msg.warn(string.format(
                "lynx_drift_correction: hard freeze detected - playback advanced < %.1fs " ..
                "over %d consecutive checks despite real time passing - signalling for an " ..
                "immediate full restart rather than nudging/drop-buffers",
                HARD_FREEZE_ADVANCE_THRESHOLD, HARD_FREEZE_CONSECUTIVE_CHECKS))
            hard_freeze_detected_at = now
        end

        -- Playback advancing by less than real time actually passed
        -- means we fell further behind live by exactly that shortfall -
        -- true regardless of whether the cause is inside mpv or
        -- somewhere upstream of it.
        estimated_drift = estimated_drift + (real_delta - playback_delta)
        if estimated_drift < 0 then
            estimated_drift = 0  -- never meaningfully "ahead" of live - clamp for sanity
        end
    end

    last_real_time = now
    last_playback_time = playback

    if hard_freeze_detected_at > 0.0 then
        -- Confirmed hard freeze pending an external restart - nudging
        -- or dropping buffers can't help when playback isn't
        -- advancing at all, so don't attempt either; just wait for
        -- lynx_app.py to act on the signal in the status file.
    elseif estimated_drift >= DROP_BUFFERS_THRESHOLD then
        if now < breaker_tripped_until then
            -- Circuit breaker active - repeated drop-buffers calls
            -- clearly aren't resolving whatever's actually wrong.
            -- Suppressed for now. With nudging disabled there is
            -- nothing else safe left to try here - just wait and let
            -- the external, already-circuit-broken playback-delay/
            -- hard-freeze monitors take over with a full restart if
            -- this doesn't clear on its own.
            if NUDGE_ENABLED and not nudge_active then
                mp.set_property("speed", NUDGE_SPEED)
                nudge_active = true
                last_action_at = now
            end
        else
            mp.msg.warn(string.format(
                "lynx_drift_correction: estimated drift %.1fs >= %.1fs threshold - " ..
                "dropping buffers for an immediate resync", estimated_drift, DROP_BUFFERS_THRESHOLD))
            mp.command("drop-buffers")
            drop_buffers_count = drop_buffers_count + 1
            reset_state()

            table.insert(recent_drop_buffers_times, now)
            local kept = {}
            for _, t in ipairs(recent_drop_buffers_times) do
                if now - t <= DROP_BUFFERS_BREAKER_WINDOW then
                    table.insert(kept, t)
                end
            end
            recent_drop_buffers_times = kept
            if #recent_drop_buffers_times >= DROP_BUFFERS_BREAKER_THRESHOLD then
                breaker_tripped_until = now + DROP_BUFFERS_BREAKER_COOLDOWN
                recent_drop_buffers_times = {}
                mp.msg.warn(string.format(
                    "lynx_drift_correction: circuit breaker tripped - %d drop-buffers calls " ..
                    "within %.0fs clearly aren't resolving whatever's wrong - backing off for %.0fs",
                    DROP_BUFFERS_BREAKER_THRESHOLD, DROP_BUFFERS_BREAKER_WINDOW,
                    DROP_BUFFERS_BREAKER_COOLDOWN))
            end
        end
    elseif NUDGE_ENABLED and estimated_drift >= NUDGE_THRESHOLD and not nudge_active then
        mp.msg.info(string.format(
            "lynx_drift_correction: estimated drift %.2fs - nudging speed to %.2fx",
            estimated_drift, NUDGE_SPEED))
        mp.set_property("speed", NUDGE_SPEED)
        nudge_active = true
        last_action_at = now
    elseif estimated_drift <= 0 and nudge_active then
        mp.msg.info("lynx_drift_correction: caught up to live - reverting to normal speed")
        mp.set_property("speed", NORMAL_SPEED)
        nudge_active = false
        last_action_at = now
    end

    write_status()
end

mp.add_periodic_timer(CHECK_INTERVAL, check_drift)

-- A fresh stream/file start (including after restart_mpv() relaunches
-- mpv entirely, which starts a brand new Lua state anyway) should
-- never carry over stale drift state from before.
mp.register_event("start-file", reset_state)

mp.msg.info("lynx_drift_correction: loaded")
