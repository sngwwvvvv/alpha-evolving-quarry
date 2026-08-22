# Alpha Evolving Quarry : Self-Improving AI Trading Desk

## 목적

단순히 전략을 작성하고 백테스트 하는 데에서 그치는 게 아니라, 데이터 수집 -&gt; 전략 작성 -&gt; 백테스트 -&gt; 실패 보완 -&gt; 수정 및 개선 -&gt; 검증 -&gt; live trading deploy -&gt; 개선 의 `swarm loop Engineering` 기법을 적용한 Crypto Trading Desk를 만드는 것

## 들어가기 전

아래 두 가지의 문서를 먼저 확인하고 아이디어를 검토할 것

- ai self-improving trading desk에 대한 전반적인 아이디어 : [https://x.com/antpalkin/status/2085431604906766385](https://x.com/antpalkin/status/2085431604906766385)
- buzz를 이용한 agent 통합 workspace에 대한 아이디어 : [https://x.com/milesdeutscher/status/2090495296765821101](https://x.com/milesdeutscher/status/2090495296765821101)
- buzz github repo : [https://github.com/block/buzz](https://github.com/block/buzz)
- grok bot 공식 document : [https://docs.x.ai/grok-bot/overview](https://docs.x.ai/grok-bot/overview)
- hermes agent 공식 document : [https://hermes-agent.nousresearch.com/docs](https://hermes-agent.nousresearch.com/docs)

## 구조

이 프로젝트는 크게 보면 2개의 레이어로 구성됨

- Backtest Layer : 과거 데이터 수집 -&gt; 전략 코드 작성 -&gt; 백테스트 실행 -&gt; 개선 -&gt; 검증
- Live Trading Layer : backtest layer에서 loop를 통해 개선되어 최종적으로 통과한 전략을 실행. 실시간 데이터 수집 -&gt; live trading -&gt; 로깅 및 개선

### 1. Backtest Layer의 Agent 구성

Research Agent, Coding Agent, Backtest Agent, Validation Agent로 구성

1. Research Agent
  - 백테스트용 과거 데이터를 수집하고 해당 데이터를 분석하는 agent
  - 데이터 수집은 다음의 subagent를 사용함
    - 1. `Fillings Subagent`
      - `Binance`에 상장된 Crypto 중, 3년 이상 상장되어 거래된 모든 crypto ticker 데이터를 수집함
      - 수집 시작기간 : 2020-01-01 또는 그 이후 상장된 코인의 경우 코인 상장일 + 6개월
      - 수집 종료기간 : 해당 subagent가 구동한 날짜
      - 수집대상 : 해당 ticker의 Daily OHLCV, 그리고 1H timeframe 의 OHLCV (추가적으로 필요한 데이터 있다면 논의 필요)
    - 2. `MacroEvent Subagent`
      - 과거의 주요 매크로이벤트 데이터 get : CPI, PPI 및 FOMC 발표 등 가상자산 가격에 주요한 영향을 미치는 이벤트
  - 데이터 분석은, daily timeframe의 fillings 데이터와 earnings event를 기반으로 market reigme과 자산별 key level 판별
2. Coding Agent
  -  Entry, Exit 시그널 생성 등 전략과 위험관리 방안 (risk-managing) 을 작성하는 에이전트
  - 전략 아이디어 자체는 사람 (user)가 제공하며, coding agent는 해당 아이디어를 바탕으로 코딩)
  - 해당 Agent가 만드는 코드 스타일은 작고 간결하며 test하기 쉬워야 함 (`ponytail` 플러그인 사용할 것)
  - 전략은 plugin 또는 skill 형태로 보관되어 다른 agent가 볼 수 있어야 함
  - 전략에는 코드와 더불어 `strategy_ledger.md` 를 만들어, 해당 원장을 보고 전략을 improve 할 수 있어야 함 (중요)
3. Backtest Agent
  - 작성된 전략을 백테스트 loop 하는 에이전트. 1H timeframe 을 기준으로 작동하며 research agent의 market reigme도 참고함
  - 한 번의 테스트 시 "하나의 자산, 6개월의 기간" 에 따라 테스트
  - 한 번에 하나의 backtest agent만 구동하는 방법 말고, 여러 개의 backtest agent를 동시에 구동해서 동시에 여러 백테스트를 진행하는 방법 어떨지 논의 필요
  - 다음의 subagent들을 사용
    - 1. `Positioning Subagent`
      - 전략에 따라 백테스트하고, 모든 trade의 포지션,entry price, exit price, 수익 또는 손실 금액, 수익 또는 손실률을 기록함
    - 2. `Ledger Subagent`
      - 기록 중 손실이 발생한  거래의 '손실이 난 이유를 분석' 하여 이유와 개선 방안을 `strategy_ledger.md`에  작성함
    - 3. `Result Subagent`
      - 백테스트 기간이 종료되면 결과를 분석 (Sharpe ratio, CAGR, Deflated Sharpe Ratio 등등) 하여 기록하고 백테스트 결과를 Backtest Agent에 보고
  - Backtest Agent는 백테스트 결과를 보고받은 후 다음의 행동을 수행
    - 통과 : 백테스트 결과가 사전에 정의한 기준 모두 충족한 경우.
      - walk-forward 절차에 따라 다음 백테스트 기간에 대한 백테스트 수행. 각각의 백테스트 기간에 대해 통과/미비 여부 판정하여 loop
      - 2020년 1월 1일부터 현재까지의 모든 백테스트를 통과한 경우 Validation Agent에게 전략을 넘김
    - 미비 : 백테스트 결과가 사전에 정의한 기준을 충족하지 못한 경우.
      - `strategy_ledger.md` 을 분석. 분석 결과에 따라 전략을 개선하여 다시 백테스트 수행 (loop)
      - 다만 loop 몇 차례 반복 시 overfit 될 수도 있을 것 같은데, 이 부분은 철저히 논의 필요함
4. Validation Agent
  - Backtest 결과 사전 정의된 기준을 모두 충족한 전략을 최종적으로 테스트하는 agent
  - OOS (validation agent 구동일 기준 최근 6개월 간의 데이터로 수행) 와 Monte-Carlo Simulation 수행
  - 수행 후 사전 정의한 기준에 따라 전략의 생존여부 결정
    - 통과 : live trading 가능. live trade로 넘어가기 전 `live_trading_ledger.md` 파일 생성하여 보관 필요.
    - 미비 : 백테스트 동안 개선에도 불구하고 최종 검증 통과 못한 케이스. 처음부터 전략 재조정하여 다시 백테스트 loop 필요

### 2. Live Trading Layer의 Agent 구성

1. Research Agent
  - 기본적으로 backtest layer의 research agent와 본질적으로 같은 기능 수행. 라이브 데이터 수집 -&gt; daily timeframe 기준 reigme과 key level 판단
  - 다만 "Sentiment Subagent"를 추가해야 함
  - `Sentiment Subagent`
    - X에서 특정 crypto자산의 언급 횟수나 X에서 확인 가능한 속보 등을 토대로 social media sentiment 분석 추가
    - 이를 market reigme 판단의 추가적인 증거로 사용
    - 왜 backtest layer에서는 사용하지 않나? =&gt; 과거의 X 데이터는 수집하기 어렵기 때문에 포기
2. Live Trading Agent
  - Backtest Layer의 Backtest Agent와 본질적으로 같은 기능 수행. 1H timeframe에서 작동하며, market reigme을 포지션 진입 시 참고
  - 모든 거래 내용을 기록하며, 특히 손실 거래의 경우 `live_trading_ledger.md`에 손실 원인과 개선 방향 분석하여 작성
  - 이후 새로운 포지션 진입 시, live trading agent는 `live_trading_ledger.md`의 이전 분석 내용을 참고하여야 함
3. Account Manage Agent
  - 라이브 계좌의 잔고, 현재 포지션, 노출 위험 등을 분석하여 실시간으로 위험 관리를 하는 에이전트



## 운영 방침

### 1. Agent

- 단순 반복 업무를 loop하는 일을 담당하는 에이전트는 Hermes (deepseek v4) 사용하며, 분석이 필요한 업무는 Grok Bot 또는 Codex 5.6 또는 kimi k3 중에 고민중인 상황  
&gt; 이 부분도 논의 필요 : agent service 어떤 것을 사용할지와 어떤 에이전트에 어떤 모델을 담당할지  
&gt; 다만 hermes를 사용할 때 이식할 모델은 무조건 deepseek v4 고정임

### 2. Workspace

- "Buzz" 사용을 기본으로 함 : 서로 다른 여러 에이전트가 협업하는 구조. Buzz를 workspace로 사용 필요 
- 다만 이를 로컬에서 돌릴지, 아니면 railway나 hostinger 와 같은 vps 서버에 deploy할지는 고민 필요한 사항임

