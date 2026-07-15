# World V2 `FactCommitted` Accepted Compiler 设计

> 状态：实现前设计；production accepted integration 仍保持关闭。
>
> 范围：只支持 `TypedChange(kind="fact_transition", transition="commit") -> FactCommitted`。
>
> 非范围：`FactCorrected`、`FactWithdrawn`、`FactCorrectionCompensated`、Action、Budget、Expression和外部副作用。

## 1. 设计结论

首个真实 accepted Adapter 应采用局部 Fact v2 合同，而不是修改全局 planner：

```text
模型输出 FactCommitIntentV2
  -> ProposalAudit 在 exact cursor 持久化
  -> trusted reader 解析 observation/evidence authority
  -> compiler 生成完整 FactCommitMaterializedPayloadV2
  -> 现有 planner 按最终 payload hash 生成 event id
  -> AcceptanceManifestV3 + exact FactCommitted effect 原子提交
  -> v2 Fact reducer 用实际 event.id/logical_time 构造 FactProjection
  -> SQLite 按相同 descriptor/reducer 重放
```

关键取舍：

- 模型只选择真正属于判断的语义字段；
- cardinality、conflict key、evidence type和全部 provenance由installed matrix与trusted reader派生；
- 模型不填写完整 assertion、after image、FactProjection、origin event ref或时间；
- v2 event payload携带完整已解析 `FactValues` 和mutation authority，但不携带 `FactProjection`；
- reducer从实际 `WorldEvent.event_id/logical_time` 确定性构造projection；
- planner仍然使用最终payload hash计算event id，不引入两阶段draft/finalize；
- legacy `FactChangedPayload` 和历史replay保持原样。

这使 Fact Adapter 成为深 Module：Interface只接收pinned proposal、change id和acceptance context；证据解析、矩阵派生、hash、manifest和replay复杂性保留在Implementation内部。

## 2. 为什么不复用现有 `FactPayload` 直接编译

现有 `FactPayload` 让模型提供：

```text
before_image / after_image
subject / predicate / cardinality / conflict_key
value_hash
assertion_binding
anchor_evidence / source_evidence
privacy
```

其中多个字段不应属于模型authority：

- cardinality由installed predicate matrix唯一决定；
- conflict key由`fact_conflict_key(subject,predicate)`唯一决定；
- evidence type由ledger中实际source event/projection决定；
- actor/channel/payload ref/content hash来自retained observation envelope；
- 完整 assertion binding应由reader解析，不能让模型复制provenance；
- after image、entity revision、origin event ref和timestamps是系统materialization结果；
- 模型提供冗余字段会产生“以哪个副本为准”的冲突面。

现有payload还缺`value_ref`和`confidence_bp`。在Adapter里猜值、给默认值或从自然语言二次抽取都会把compiler变成新的推理模型。因此v1保持unsupported，新增最小语义intent v2。

## 3. Scope与fail-closed合同

唯一可启用key：

```text
proposal registry: world-v2-proposals.2
change kind:       fact_transition
transition:        commit
payload schema:    fact_commit_intent.v2
payload version:   2
owned event type:  FactCommitted
output contract:   fact-commit-materialized.2
```

以下情况稳定拒绝：

- transition为correct/withdraw/compensate或未知值；
- kind不是fact_transition；
- v1 FactPayload或未知schema/version；
- Adapter输出非FactCommitted；
- authority不在pinned ProposalAudit中；
- evidence在cursor之后出现或不能解析；
- 任何字段需要猜测、fallback或LLM补全；
- production descriptor/callable/digest不在sealed install manifest；
- test-only authority尝试进入recorder；
- manifest/effect/reducer/replay任一环节不能重新证明相同authority。

## 4. Module与Interface

### 4.1 production Interface

```python
class InstalledDomainCompilerRegistry:
    def compile_verified(
        self,
        *,
        proposal_authority: PinnedProposalAuthorityHandle,
        change_id: str,
        context: AcceptanceCompilationContext,
    ) -> VerifiedCompiledEffectHandle: ...
```

调用者不传：

- Adapter；
- compiler key或event type；
- TypedChange副本；
- observation/provenance副本；
- FactValues或FactProjection；
- event id、timestamps或payload hash。

registry从pinned ProposalAudit按change id取得唯一TypedChange，选择sealed Adapter，并返回不可序列化的verified handle。

### 4.2 内部resolver seam

```python
class FactCommitAuthorityResolver(Protocol):
    def resolve(
        self,
        *,
        proposal_authority: PinnedProposalAuthorityHandle,
        change: TypedChange,
        context: AcceptanceCompilationContext,
    ) -> ResolvedFactCommitAuthority: ...
```

resolver负责从exact ledger prefix解析authority。它返回内部frozen capability，不返回可由调用者持久化后重新冒充authority的DTO。

### 4.3 内部Adapter seam

```python
class FactCommitAdapter(Protocol):
    def compile(
        self,
        *,
        authority: ResolvedFactCommitAuthority,
        context: AcceptanceCompilationContext,
    ) -> DomainPayloadDraft: ...

    def reverse_verify(
        self,
        actual: DomainPayloadDraft,
        *,
        authority: ResolvedFactCommitAuthority,
        context: AcceptanceCompilationContext,
    ) -> None: ...
```

Adapter一次生成最终canonical `FactCommitMaterializedPayloadV2`。它不需要event id；projection origin与时间由reducer从event envelope构造。

## 5. Proposal schema：`FactCommitIntentV2`

### 5.1 intent只包含模型必须选择的字段

```python
class FactEvidenceUseV2(FrozenModel):
    evidence_ref: BoundedRef
    purpose: Literal[
        "current_fact",
        "past_experience",
        "future_plan",
        "private_hypothesis",
        "conversation_continuity",
    ]
    anchor: bool


class FactCommitIntentV2(FrozenModel):
    subject_ref: BoundedRef
    predicate_code: BoundedLabel
    value_ref: BoundedRef
    value_hash: Sha256Prefixed
    assertion_source_ref: BoundedRef
    evidence_uses: tuple[FactEvidenceUseV2, ...]  # 1..64
    confidence_bp: int                            # 1..10_000
    privacy_class: PrivacyClass
```

模型必须选择：

- 谁/什么是subject；
- 提出哪个predicate；
- 值的canonical ref/hash；
- 哪一条已给模型的source是直接assertion source；
- 每条evidence在该claim中的用途及是否为anchor；
- confidence和privacy。

模型明确不选择：

- fact id；
- entity revision；
- cardinality；
- conflict key；
- evidence type；
- evidence revision/hash；
- actor/channel/payload ref/content hash；
- 完整`FactAssertionBinding`；
- policy refs；
- transition id；
- after image、origin、event ref或timestamps。

### 5.2 TypedChange外围字段

`TypedChange`仍保留通用envelope，但其系统字段必须由proposal normalizer确定性生成：

| TypedChange字段 | 来源/合同 |
|---|---|
| `change_id` | proposal builder确定性identity；模型不得覆盖 |
| `kind` | 固定`fact_transition` |
| `target_id` | `fact-id.2`合同从world/proposal/change/intent hash确定性派生 |
| `expected_entity_revision` | 固定`0` |
| `transition` | 固定`commit` |
| `evidence_refs` | `evidence_uses.evidence_ref`按canonical顺序派生 |
| `preconditions` | sealed commit precondition allowlist；通常为空 |
| `policy_refs` | installed Fact commit policy refs；模型不得填写 |
| `payload` | `fact_commit_intent.v2` canonical bytes/hash |

建议fact id合同：

```text
fact_id = "fact:" + sha256(canonical_json({
  "contract": "fact-id.2",
  "world_id": world_id,
  "proposal_id": proposal_id,
  "change_id": change_id,
  "intent_hash": typed_payload_hash
}))
```

这样模型只表达事实，系统负责entity identity；同一proposal change重试不会漂移。

### 5.3 intent schema不允许冗余provenance

`extra="forbid"`。若模型输出以下字段，schema必须拒绝，而不是忽略：

```text
cardinality
conflict_key
evidence_type
source_world_revision
immutable_hash
actor_ref
channel
payload_ref
content_payload_hash
assertion_binding
after_image
fact_projection
accepted_event_ref
committed_at / updated_at
```

## 6. Observation authority扩展

### 6.1 当前缺口

当前`MessageObservationRef`已经保留：

```text
observation_id
source / source_event_id
content_payload_hash / event_payload_hash
world_revision
actor / channel / payload_ref
```

但actor/channel/payload_ref为了旧state迁移仍是optional；新的Fact commit不能接受缺失值。当前`OperatorObservationRef`只有observation id/hash，缺少明确的committed revision/event ref。reader若只拿到宽projection，不能证明operator evidence在pinned cursor可见。

### 6.2 新写入必须保留完整message envelope

新的message observation authority必须满足：

- actor、channel、payload_ref非空；
- content payload hash为canonical 64-hex；
- retained event payload hash为canonical 64-hex；
- world revision和ledger sequence可定位；
- source event identity可从ledger prefix复核；
- observation ref与原始`ObservationRecorded` event bytes一致。

legacy缺失actor/channel/payload_ref的message observation不能授权新的FactCommit v2；它可以继续用于legacy replay。

### 6.3 operator observation authority扩展

新增或扩展内部projection，至少保留：

```python
class OperatorObservationAuthorityV2(FrozenModel):
    observation_id: str
    observation_hash: Sha256Hex
    event_ref: str
    event_payload_hash: Sha256Hex
    committed_world_revision: int
    ledger_sequence: int
```

reader必须从ledger event重建/复核该projection。不能仅因`OperatorObservationRef.observation_id`存在就授予authority。

### 6.4 通用resolved evidence

reader把intent中的`FactEvidenceUseV2`解析为：

```python
class ResolvedFactEvidenceV2(FrozenModel):
    ref_id: str
    evidence_type: Literal[
        "observed_message",
        "operator_observation",
        "committed_world_event",
        "settled_world_event",
        "settled_external_result",
        "committed_fact",
        "committed_experience",
    ]
    claim_purpose: EvidenceClaimPurpose
    source_world_revision: int | None
    immutable_hash: Sha256Hex
    anchor: bool
```

`evidence_type`和provenance只能由reader根据实际authority source派生。模型的`purpose/anchor`仍要经过installed Fact commit matrix，不能仅因模型选择就被接受。

## 7. Trusted reader解析合同

### 7.1 固定输入

reader只能读取：

- 同一issuer签发的`PinnedProposalAuthorityHandle`；
- handle绑定的world、完整cursor和ledger prefix；
- audit内唯一匹配的TypedChange/FactCommitIntentV2；
- cursor之前已提交的observation/evidence/fact projections；
- sealed predicate、evidence-use、privacy和policy matrix；
- acceptance context的world/cursor/logical time。

不得读取当前head、cursor之后的新消息、mutable cache、向量检索结果或LLM补全。

### 7.2 assertion source解析

若`assertion_source_ref`解析为message observation，reader生成：

```python
FactAssertionBinding(
    source_kind="observed_message",
    source_ref=observation_id,
    asserted_subject_ref=intent.subject_ref,
    actor_ref=retained.actor,
    channel=retained.channel,
    payload_ref=retained.payload_ref,
    content_payload_hash=retained.content_payload_hash,
)
```

若解析为operator observation：

```python
FactAssertionBinding(
    source_kind="operator_observation",
    source_ref=observation_id,
    asserted_subject_ref=intent.subject_ref,
    actor_ref=None,
    channel=None,
    payload_ref=None,
    content_payload_hash=observation_hash,
)
```

同一ref解析到零个或多个authority、source类型不允许、retained envelope不完整、hash/revision不匹配均拒绝。

### 7.3 evidence-use matrix

建议sealed matrix至少规定：

| source type | commit允许purpose | 可作为anchor | 可作为assertion source |
|---|---|---:|---:|
| observed message | current_fact | 是 | 是 |
| operator observation | current_fact | 是 | 是 |
| committed fact | current_fact / conversation_continuity | 是 | 否 |
| committed experience | past_experience / conversation_continuity | 否 | 否 |
| committed/settled world event | current_fact / past_experience | 依predicate policy | 否 |
| settled external result | current_fact | 依predicate policy | 否 |

最小commit约束：

- assertion source必须出现在evidence uses中；
- 其purpose必须为`current_fact`；
- assertion source必须`anchor=True`；
- 至少一个anchor；
- `(evidence_ref,purpose)`唯一；
- anchors是source evidence的标记子集，不单独复制一份输入；
- 每条evidence在pinned cursor可见；
- evidence type/purpose/anchor组合被matrix允许。

### 7.4 predicate/cardinality/conflict派生

reader/compiler使用sealed predicate matrix，例如：

```text
location.current             -> single
profile.display_name         -> single
profile.timezone             -> single
preference.likes             -> set
preference.dislikes          -> set
relationship.affiliation     -> set
```

`cardinality`只由该表派生。未知predicate拒绝。`conflict_key`只由现有`fact_conflict_key(subject_ref,predicate_code)`计算。模型无法声明或覆盖二者。

### 7.5 privacy与confidence

- confidence由模型选择，但必须在1..10,000；compiler不得默认；
- privacy由模型选择，但reader按resolved evidence type/purpose matrix计算最低privacy；
- 模型给出的privacy更严格可以接受；更宽则拒绝；
- policy/matrix digest进入sealed descriptor和durable manifest metadata。

### 7.6 冲突预检

在pinned cursor预检：

- target fact id不存在；
- 不存在相同active semantic fingerprint；
- 不存在同conflict key/cardinality/value hash identity；
- single slot没有其他active fact；
- authoritative logical time已建立。

reducer仍重复相同检查，ledger recorder使用world/deliberation CAS抵抗TOCTOU。

## 8. Output schema：`FactCommitMaterializedPayloadV2`

### 8.1 payload只含reducer所需的已解析authority

```python
class FactCommitMaterializedPayloadV2(FrozenModel):
    payload_contract: Literal["fact-commit-materialized.2"]

    change_id: str
    transition_id: str
    fact_id: str
    expected_entity_revision: Literal[0]

    evidence_refs: tuple[EvidenceRef, ...]
    policy_refs: tuple[str, ...]

    acceptance_id: str
    proposal_id: str
    evaluated_world_revision: int
    full_change_authority_hash: Sha256Hex

    values: FactValues
    materialized_change_hash: Sha256Hex
```

它明确不包含：

```text
operation                # event type已固定commit
fact_before
FactProjection
FactOrigin
origin.accepted_event_ref
entity_revision          # reducer固定为1
semantic_fingerprint     # reducer从values/policy确定性计算
committed_at / updated_at
compensates_transition_id
```

### 8.2 legacy payload保持不变

legacy `FactChangedPayload`、`FactProposalProjection`、`fact_mutation_hash`和历史event bytes不修改。`FactCommitted` event catalog使用显式payload discriminator：

```text
payload_contract == fact-commit-materialized.2
  -> FactCommitMaterializedPayloadV2
payload_contract字段不存在，且同commit满足legacy authority合同
  -> FactChangedPayload
unknown payload_contract或混合shape
  -> reject，不允许fallback
```

新production recorder只允许Fact payload v2 discriminator；legacy reader只用于已存在历史event。禁止新写入伪装成legacy以绕过manifest-v3。

### 8.3 materialized hash

```text
materialized_change_hash = sha256(canonical_json({
  "contract": "fact-commit-materialized-hash.2",
  "payload": payload excluding materialized_change_hash,
}))
```

它不是：

- `full_change_authority_hash`；
- legacy `fact_mutation_hash`；
- WorldEvent payload hash；
- Fact semantic fingerprint；
- manifest hash。

### 8.4 transition id

```text
transition_id = "fact-transition:" + sha256(canonical_json({
  "contract": "fact-commit-transition-id.2",
  "world_id": world_id,
  "proposal_id": proposal_id,
  "change_id": change_id,
  "full_change_authority_hash": full_change_authority_hash,
  "fact_id": fact_id,
  "expected_entity_revision": 0
}))
```

同一proposal change重试稳定，其他world/proposal/change/authority变化时不同。

## 9. 逐字段映射

### 9.1 TypedChange/Intent到materialized payload

| 输出字段 | 输入/派生 | 验证 |
|---|---|---|
| `payload_contract` | installed output contract | 固定`fact-commit-materialized.2` |
| `change_id` | `TypedChange.change_id` | 与ProposalAudit/manifest summary一致 |
| `transition_id` | 8.4 deterministic contract | world内唯一 |
| `fact_id` | `TypedChange.target_id` | 等于proposal builder按fact-id.2派生值 |
| `expected_entity_revision` | `TypedChange.expected_entity_revision` | 固定0 |
| `evidence_refs` | reader解析`intent.evidence_uses` | 完整type/purpose/revision/hash，canonical且唯一 |
| `policy_refs` | `TypedChange.policy_refs` | 等于sealed descriptor installed refs |
| `acceptance_id` | acceptance context | 与manifest一致 |
| `proposal_id` | pinned ProposalAudit | 不信调用者副本 |
| `evaluated_world_revision` | proposal + pinned cursor | 必须相等 |
| `full_change_authority_hash` | manifest-change-authority.1对完整TypedChange派生 | 与effect authority ref一致 |
| `values` | 9.2 | 完整已解析FactValues |
| `materialized_change_hash` | 8.3 | schema/reverse verifier重算 |

### 9.2 Intent到FactValues

| FactValues字段 | 输入/派生 | 验证 |
|---|---|---|
| `subject_ref` | `intent.subject_ref` | assertion subject相同 |
| `predicate_code` | `intent.predicate_code` | installed predicate |
| `cardinality` | installed predicate matrix | 模型无覆盖字段 |
| `conflict_key` | `fact_conflict_key(subject,predicate)` | deterministic |
| `value_ref` | `intent.value_ref` | 非空、受intent hash覆盖 |
| `value_hash` | `intent.value_hash`去除已验证`sha256:`前缀 | 64位lower hex；不二次hash |
| `assertion_binding` | reader解析`assertion_source_ref` | 与retained observation逐字段一致 |
| `anchor_evidence_refs` | resolved evidence中`anchor=True` | source子集，包含assertion source |
| `source_evidence_refs` |全部resolved evidence | canonical、唯一、全部可见 |
| `confidence_bp` | `intent.confidence_bp` | 1..10,000；无默认 |
| `privacy_class` | `intent.privacy_class` | 不低于matrix最小值 |
| `status` | commit contract | 固定active |
| `withdrawal_reason_code` | commit contract | 固定None |
| `withdrawal_evidence_ref` | commit contract | 固定None |

### 9.3 reducer构造FactProjection

v2 reducer在收到实际`FactCommitted` WorldEvent时构造：

```python
origin = FactOrigin(
    change_id=payload.change_id,
    transition_id=payload.transition_id,
    policy_refs=payload.policy_refs,
    accepted_event_ref=event.event_id,
)

projection = FactProjection(
    fact_id=payload.fact_id,
    entity_revision=1,
    semantic_fingerprint=fact_semantic_fingerprint(
        subject_ref=payload.values.subject_ref,
        predicate_code=payload.values.predicate_code,
        cardinality=payload.values.cardinality,
        conflict_key=payload.values.conflict_key,
        value_hash=payload.values.value_hash,
        assertion_binding=payload.values.assertion_binding,
        anchor_evidence_refs=payload.values.anchor_evidence_refs,
        policy_refs=payload.policy_refs,
    ),
    values=payload.values,
    origin=origin,
    committed_at=event.logical_time,
    updated_at=event.logical_time,
)
```

因此`origin.accepted_event_ref`自然等于实际event id，没有self-reference进入payload。

## 10. Planner与hash关系

### 10.1 不修改全局planner identity

Adapter在planner之前已产生最终canonical materialized payload。foundation继续：

```text
final payload bytes
  -> final payload hash
  -> planner event identity（包含payload hash）
  -> manifest authorized effect
```

不新增compile draft、event-id预分配或finalize步骤；不删除payload hash与event id的绑定。

### 10.2 hash/digest矩阵

| 名称 | 材料 | 作用 |
|---|---|---|
| intent payload hash | canonical FactCommitIntentV2 | TypedChange payload绑定 |
| full change authority hash | domain-separated完整TypedChange | manifest effect authority |
| proposal hash |完整canonical ProposalEnvelope | proposal identity |
| proposal event payload hash | ProposalRecorded event bytes | ledger audit identity |
| materialized change hash | v2 materialized payload排除self hash | concrete mutation自校验 |
| event payload hash |完整canonical materialized payload | WorldEvent与manifest effect绑定 |
| event id |现有planner identity，包含event payload hash及authority/compiler metadata | effect identity |
| semantic fingerprint | resolved FactValues anchors + policies | Fact语义去重 |
| compiler/resolver/verifier/output/dependency digests | sealed artifacts |实现合同 |
| registry digest |完整coverage descriptor manifest |安装集合 |
| manifest hash |完整manifest排除self hash | accepted batch authority |

必须满足：

```text
effect.authority_hash == derived full_change_authority_hash
effect.event_type      == FactCommitted
effect.event_id        == planner derived event id
effect.payload_hash    == sha256(final materialized payload bytes)
payload.materialized_change_hash == recomputed materialized hash
reducer projection.origin.accepted_event_ref == actual event.event_id
reducer projection timestamps == actual event.logical_time
reducer semantic_fingerprint == recomputed fingerprint
```

## 11. Sealed install descriptor

production descriptor至少包含：

```yaml
descriptor_ref: compiler-install:fact-commit.2
proposal_schema_registry: world-v2-proposals.2
change_kind: fact_transition
transition: commit
payload_schema: fact_commit_intent.v2
payload_version: 2
event_types: [FactCommitted]
output_contract: fact-commit-materialized.2
compiler_ref/digest: ...
resolver_ref/digest: ...
reverse_verifier_ref/digest: ...
output_contract_ref/digest: ...
predicate_matrix_ref/digest: ...
evidence_use_matrix_ref/digest: ...
privacy_matrix_ref/digest: ...
observation_authority_contract_ref/digest: ...
fact_policy_refs: [...]
dependency_digests: [...]
registry_version/digest: ...
reducer_bundle: ...
event_catalog_digest: ...
```

production registry只能从build-time sealed allowlist建立`descriptor -> exact callable/schema/matrix`映射。registration自报的64位digest不能成为production authority。任一artifact不匹配时key保持unsupported。

test registry可以注入fake Adapter，但identity必须带test_only，不能materialize WorldEvent，recorder必须拒绝；test key也应遵守Fact→FactCommitted ownership。

## 12. Manifest-v2 batch与durable metadata

### 12.1 atomic batch

当前冻结的manifest-v2仍保持既有语义；新的accepted生产写入使用manifest-v3：

```text
commit index 0: AcceptanceRecorded(AcceptanceManifestV3 status=accepted)
commit index 1: FactCommitted(FactCommitMaterializedPayloadV2)
```

manifest effect ordinal为0，对应commit中AcceptanceRecorded之后的第一个effect。

拒绝：

- 缺失、额外、重复或乱序effect；
- event id/type/payload hash/ordinal不一致；
- authority ref不是derived full change authority；
- proposal event未在接受前提交；
- world/cursor/revision不一致；
- 同一change authority重复消费；
- test-only plan；
- stale ledger CAS；
- reducer失败后的部分提交。

### 12.2 durable compiler metadata

新的`AcceptanceAuthorizedEffectV3`必须持久化execution plan里的完整compiler authority metadata，并让manifest hash覆盖：

```text
compiler key及payload version
sealed descriptor ref/digest
compiler/resolver/reverse verifier/output contract refs+digests
matrix/observation authority/dependency digests
registry version/digest
reducer bundle/event catalog digest
```

也可由`reducer_bundle + event_type + payload_contract`唯一指向历史sealed descriptor，但该映射必须历史不可变、受manifest或ledger schema version绑定，并测试无一对多。只保存在内存Handle/plan不够。

### 12.3 acceptance projection

manifest-v3不得压缩为legacy单-change decision后重新作为authority。`AcceptanceManifestRefV3` projection应保留manifest hash/version、proposal summaries、effects、descriptor metadata和commit ordinals。若生成legacy-shaped查询view，它只能只读。manifest-v2 bytes/hash/parser/replay保持冻结，不得upcast成v3。

## 13. Reducer与replay迁移

### 13.1 双轨payload和authority

Fact reducer显式分流：

```text
legacy payload:
  FactChangedPayload
  + legacy FactProposalProjection
  + legacy AcceptanceRecorded adjacency

v2 payload_contract=fact-commit-materialized.2:
  ProposalAudit v2
  + accepted manifest exact effect
  + historical sealed descriptor
  + materialized payload reverse verification
```

v2失败绝不回退legacy；新写入禁止legacy shape。

### 13.2 v2 reducer不变量

在构造projection前验证：

- event type为FactCommitted；
- expected entity revision为0；
- materialized hash、full change hash和manifest cross-hash；
- descriptor/matrix/policy digests；
- values schema、evidence和assertion provenance；
- predicate/cardinality/conflict key；
- privacy；
- fact id、semantic、content和single-slot冲突；
- transition id唯一；
- event logical time等于authoritative current logical time。

然后只用payload values + actual event envelope构造projection和transition history。

### 13.3 SQLite replay

SQLite必须保存原始event bytes、commit grouping和ordinal。reopen/replay：

1. 重建ProposalAudit；
2. 验证manifest hash和exact batch；
3. 解析historical sealed descriptor；
4. 重新derive TypedChange/full-change authority；
5. 重新解析observation authority或验证materialized provenance仍指向相同历史sources；
6. 运行v2 reverse verifier；
7. reducer用stored event id/logical time构造projection；
8. 比较facts/transitions/acceptance projection/semantic hash。

旧descriptor缺失时稳定`replay_descriptor_missing`停止，不使用当前latest替代。

## 14. 稳定错误码

外部Fact码前缀：`fact_acceptance_compiler.`；foundation继续`acceptance_compiler.`。

| code | 含义 |
|---|---|
| `unsupported_transition` | 非commit或未安装key |
| `schema_mismatch` | proposal registry/payload version/contract错误 |
| `proposal_authority_mismatch` | audit/change/world/cursor不一致 |
| `intent_noncanonical` | intent typed round-trip不一致 |
| `target_derivation_mismatch` | TypedChange target不是fact-id.2派生值 |
| `revision_mismatch` | expected revision不是0 |
| `evidence_use_invalid` | purpose/anchor组合不被matrix允许 |
| `evidence_unresolved` | ref在pinned cursor无法唯一解析 |
| `evidence_hash_mismatch` | ledger revision/hash/provenance不一致 |
| `observation_authority_incomplete` | message/operator retained authority不足 |
| `assertion_source_invalid` | source未作为current_fact anchor或类型不允许 |
| `predicate_uninstalled` | predicate无installed cardinality |
| `fact_conflict` | fact id/semantic/content/single-slot冲突 |
| `privacy_rejected` | privacy低于matrix最低要求 |
| `confidence_invalid` | confidence缺失或越界 |
| `policy_mismatch` | policy/matrix descriptor不匹配 |
| `materialized_payload_invalid` | output schema/typed canonical错误 |
| `materialized_hash_mismatch` | concrete self hash错误 |
| `manifest_binding_mismatch` | effect与manifest不一致 |
| `descriptor_missing` | production descriptor未安装 |
| `descriptor_digest_mismatch` | callable/schema/matrix digest错误 |
| `stale_cursor` | resolver cursor或ledger CAS stale |
| `atomic_commit_failed` | batch整体回滚 |
| `replay_descriptor_missing` |历史descriptor不可用 |
| `replay_diverged` |首次projection与replay不同 |

Adapter普通`Exception`映射为stable invalid-output/reverse-verification错误；不捕获`BaseException`；detail不得泄露原始私密evidence或模型输出。

## 15. 测试矩阵（36项）

### A. Intent与系统派生（1–8）

1. observed-message intent成功生成唯一materialized payload。
2. operator-observation intent成功生成唯一materialized payload。
3. intent额外填写cardinality/conflict/provenance/after image时schema拒绝。
4. value_ref、value hash、assertion source、confidence、privacy任一缺失时拒绝。
5. target id按fact-id.2稳定派生；篡改target拒绝。
6. expected revision非0、kind/transition不匹配分别拒绝。
7. installed predicate分别派生single/set，未知predicate拒绝。
8. conflict key只由subject/predicate确定性派生。

### B. Observation与evidence authority（9–16）

9. evidence只在pinned cursor之后出现时拒绝。
10. message event/world revision/hash任一篡改时拒绝。
11. message actor/channel/payload ref/content hash缺失或篡改时拒绝。
12. operator event ref/revision/payload hash/observation hash任一错误时拒绝。
13. 同一ref解析到零个或多个source时拒绝。
14. assertion source未进入uses、非current_fact或非anchor时拒绝。
15. duplicate evidence use、非法purpose/source-type、非法anchor组合分别拒绝。
16. privacy过宽、policy/matrix digest不一致分别拒绝。

### C. Materialized payload与hash（17–23）

17. 所有FactValues字段逐项等于intent或reader/matrix派生值。
18. materialized payload不包含FactProjection/origin/event ref/time字段。
19. transition id对重试稳定，对world/proposal/change/authority变化敏感。
20. full change hash、materialized hash、event payload hash分别篡改时拒绝。
21. typed output省略default、使用alias、未知字段或非canonical bytes时拒绝。
22. hostile nested model_construct evidence/assertion/compiler key被strict rehydrate拒绝。
23. 现有planner event id仍随最终payload hash变化；不需要finalize步骤。

### D. Registry、预算与隔离（24–28）

24. production只接受sealed Fact commit descriptor，自报callable/digest拒绝。
25. Fact key输出非FactCommitted、重复key/event owner拒绝。
26. v1及correct/withdraw/compensate和其他keys保持unsupported。
27. payload bytes/depth/nodes/integer、dependency count、metadata总预算超限拒绝。
28. test-only plan不能进入recorder或materialize WorldEvent。

### E. Batch与reducer（29–33）

29. `[accepted manifest, FactCommitted v2]`原子成功，reducer生成一个Fact/transition。
30. 缺失/额外/重复/乱序effect或id/type/hash/ordinal不一致时整批零写入。
31. reducer生成origin ref等于实际event id，timestamps等于event logical time。
32. duplicate fact/semantic/content/single-slot和stale CAS分别整批回滚。
33. v2失败不回退legacy；legacy历史payload仍原样replay。

### F. SQLite replay（34–36）

34. in-memory、SQLite首次projection及reopen replay完全一致。
35. proposal/manifest/materialized event/commit ordinal任一数据库篡改时fail closed。
36. historical descriptor缺失、atomic interruption、同acceptance重试分别得到稳定停止、零部分状态、exact-once结果。

## 16. 实施顺序

1. 新增FactCommitIntentV2及closed proposal registry v2；v1保持unsupported。
2. 扩展message/operator observation authority projection和historical reader。
3. 实现sealed predicate/evidence-use/privacy matrices及digests。
4. 实现FactCommitAuthorityResolver、materialized schema、Adapter和reverse verifier，保持inert。
5. 扩展manifest durable descriptor metadata和accepted exact batch validator。
6. 实现manifest-aware atomic recorder，这是唯一WorldEvent materialization seam。
7. 实现FactCommitted v2 payload discriminator和reducer；legacy路径隔离保留。
8. 实现SQLite historical descriptor replay。
9. 通过全部gate后只启用fact_transition/commit。

## 17. 启用门槛

- [ ] `FactCommitIntentV2`只含模型必须选择的八类语义数据；extra forbidden；
- [ ] cardinality/conflict/evidence type/provenance/assertion均由matrix+trusted reader派生；
- [ ] message新authority完整，legacy optional envelope不能授权v2；
- [ ] operator authority包含event ref/hash/revision/sequence并由ledger复核；
- [ ] `FactCommitMaterializedPayloadV2`不含projection/origin event ref/time；
- [ ] reducer只用actual event id/logical time构造projection origin/timestamps；
- [ ] 现有planner继续绑定final payload hash，无全局identity改动；
- [ ] legacy FactChangedPayload/event bytes/replay完全不变；
- [ ] independent reverse verifier重算intent映射、reader authority、matrices和所有hash；
- [ ] sealed descriptor绑定compiler/resolver/verifier/schema/matrices/observation contract/reducer；
- [ ] production registry拒绝调用者registration；
- [ ] manifest持久覆盖historical descriptor metadata；
- [ ] accepted manifest + exact effects同commit原子验证；
- [ ] recorder是唯一materialization seam并拒绝test-only；
- [ ] v2 reducer失败绝不回退legacy；
- [ ] SQLite按historical descriptor重放一致；
- [ ] 第15节36项测试、Fact authority、ledger、upcasting、SQLite及fuzz suites全部通过；
- [ ] 仅Fact commit key启用，其余accepted keys保持关闭。

任一项未完成时：

```text
Fact commit coverage = unsupported
ACCEPTED_MANIFEST_INTEGRATION_ENABLED = False
无production event materialization
```

## 18. 维护规则

- 新predicate、evidence-use或privacy规则必须版本化matrix并更新digest/tests；
- observation authority合同变化必须版本化，不能放宽历史optional字段；
- intent/materialized/hash/id/reducer合同变化必须产生新descriptor；
- 历史descriptor/reducer bundle必须可重放，或通过显式verified migration event迁移；
- compiler/resolver不得调用LLM、网络、当前head或非确定性随机；
- 受控随机和人类拟真决策发生在Proposal生成阶段，accepted path只执行已审计authority；
- 错误码属于Interface，不能用异常文案代替；
- 未来correct/withdraw/compensate必须分别设计，不能扩成多分支万能Adapter。
