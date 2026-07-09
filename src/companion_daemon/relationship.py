from companion_daemon.models import MoodState, RelationshipStage


def affection_score(state: MoodState) -> int:
    love = state.emotion_vector.get("love", 10)
    trust_emotion = state.emotion_vector.get("trust", 20)
    return round(
        (state.intimacy * 0.45)
        + (state.trust * 0.20)
        + (state.attachment * 0.15)
        + (love * 0.12)
        + (trust_emotion * 0.08)
    )


def advance_relationship(
    state: MoodState,
    *,
    user_message_count: int,
    key_event_score: int = 0,
) -> MoodState:
    next_stage = stage_for_scores(
        state.intimacy,
        state.trust,
        user_message_count + key_event_score,
    )
    if next_stage == state.relationship_stage:
        return state
    return state.model_copy(update={"relationship_stage": next_stage})


def key_event_bonus(events: list[str]) -> int:
    score = 0
    for event in events:
        if "记得" in event or "小事" in event:
            score += 3
        elif "认真修复" in event or "认真" in event:
            score += 4
        elif "解释" in event or "没回" in event:
            score += 2
        else:
            score += 1
    return min(18, score)


def stage_for_scores(intimacy: int, trust: int, user_message_count: int) -> RelationshipStage:
    if user_message_count >= 120 and intimacy >= 75 and trust >= 70:
        return "lover"
    if user_message_count >= 70 and intimacy >= 55 and trust >= 55:
        return "ambiguous"
    if user_message_count >= 35 and intimacy >= 35 and trust >= 45:
        return "close_friend"
    if user_message_count >= 12 and intimacy >= 18 and trust >= 25:
        return "friend"
    if user_message_count >= 4 and trust >= 18:
        return "acquaintance"
    return "stranger"


def relationship_instruction(stage: RelationshipStage) -> str:
    return {
        "stranger": "刚认识。礼貌、自然、保留边界，不使用恋人语气。",
        "acquaintance": "开始熟悉。可以轻松一点，但仍不过度暧昧。",
        "friend": "普通朋友。有一些默契，可以自然关心。",
        "close_friend": "亲近朋友。可以更坦诚，有轻微小脾气。",
        "ambiguous": "暧昧阶段。可以有克制的想念和在意，但不要夸张。",
        "lover": "恋人阶段。可以亲密、撒娇和吃一点小醋，但仍保持边界。",
    }[stage]


def relationship_status_line(state: MoodState) -> str:
    return (
        f"好感度: {affection_score(state)}/100；"
        f"亲密度: {state.intimacy}/100；"
        f"信任感: {state.trust}/100；"
        f"依恋感: {state.attachment}/100；"
        f"当前关系: {state.relationship_stage}。"
    )


def proactive_cooldown_minutes(state: MoodState, base_minutes: int) -> int:
    stage_multiplier = {
        "stranger": 4.0,
        "acquaintance": 3.0,
        "friend": 2.0,
        "close_friend": 1.35,
        "ambiguous": 1.0,
        "lover": 0.75,
    }[state.relationship_stage]
    mood_multiplier = {
        "happy": 0.9,
        "miss_you": 0.75,
        "worried": 0.85,
        "jealous_soft": 0.85,
        "sulking": 1.15,
        "sleepy": 1.35,
        "guarded": 1.75,
        "hurt": 2.2,
        "affectionate": 0.75,
        "curious": 0.9,
        "calm": 1.0,
    }[state.mood]
    attachment_multiplier = max(0.65, 1.0 - (state.attachment / 250))
    boundary_multiplier = 1.0 + (state.boundary_level / 80)
    initiative_multiplier = max(0.65, 1.0 - (state.initiative / 250))
    ghost_multiplier = 1.0 + (emotion_ghost_window_hours(state) / 5)
    minutes = round(
        base_minutes
        * stage_multiplier
        * mood_multiplier
        * attachment_multiplier
        * boundary_multiplier
        * initiative_multiplier
        * ghost_multiplier
    )
    return max(12, min(360, minutes))


def life_event_probability(state: MoodState) -> float:
    stage_base = {
        "stranger": 0.02,
        "acquaintance": 0.04,
        "friend": 0.08,
        "close_friend": 0.13,
        "ambiguous": 0.20,
        "lover": 0.28,
    }[state.relationship_stage]
    mood_bonus = {
        "happy": 0.08,
        "miss_you": 0.05,
        "worried": 0.03,
        "jealous_soft": 0.02,
        "sulking": -0.04,
        "sleepy": -0.03,
        "guarded": -0.08,
        "hurt": -0.12,
        "affectionate": 0.09,
        "curious": 0.03,
        "calm": 0.0,
    }[state.mood]
    score_bonus = (affection_score(state) / 100) * 0.10
    boundary_penalty = state.boundary_level / 250
    emotion = state.emotion_vector
    initiative_bonus = state.initiative / 500
    emotion_bonus = (
        emotion.get("anticipation", 0) / 600
        + emotion.get("love", 0) / 700
        + emotion.get("sadness", 0) / 900
    )
    aversion_penalty = (emotion.get("anger", 0) + emotion.get("disgust", 0)) / 500
    probability = stage_base + mood_bonus + score_bonus + initiative_bonus + emotion_bonus - boundary_penalty - aversion_penalty
    return max(0.01, min(0.45, probability))


def emotion_ghost_window_hours(state: MoodState) -> float:
    anger = state.emotion_vector.get("anger", 0)
    disgust = state.emotion_vector.get("disgust", 0)
    if anger >= 85 and disgust >= 85:
        return 12
    if anger >= 85 or disgust >= 85:
        return 8
    if anger >= 70 or disgust >= 70:
        return 4
    if anger >= 50 or disgust >= 50:
        return 1.5
    return 0
