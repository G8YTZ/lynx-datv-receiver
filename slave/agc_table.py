# From wiki.batc.org.uk/MiniTiouner_Power_Level_Indication, measured by
# Dave G8GKQ against the QO-100 beacon, July 2021. The "Portsdown
# Indication" column - Ryde uses the identical calibration.
AGC2_TO_DBM = [(3200,-97),(2740,-96),(2560,-95),(2380,-94),(2200,-93),(2020,-92),
               (1840,-91),(1660,-90),(1480,-89),(1300,-88),(1140,-87),(1000,-86),
               (880,-85),(780,-84),(700,-83),(625,-82),(560,-81),(500,-80),
               (450,-79),(400,-78),(360,-77),(325,-76),(290,-75),(255,-74),
               (225,-73),(200,-72),(182,-71)]
AGC1_TO_DBM = [(1,-70),(10,-69),(21800,-68),(25100,-67),(27100,-66),(28100,-65),
               (28900,-64),(29600,-63),(30100,-62),(30550,-61),(31000,-60),
               (31350,-59),(31700,-58),(32050,-57),(32400,-56),(32700,-55),
               (33000,-54),(33300,-53),(33600,-52),(33900,-51),(34200,-50),
               (34500,-49),(34750,-48),(35000,-47),(35250,-46),(35500,-45),
               (35750,-44),(36000,-43),(36200,-42),(36400,-41),(36600,-40),
               (36800,-39),(37000,-38),(37200,-37),(37400,-36),(37600,-35)]

def _interp(x, table, descending=False):
    pts = sorted(table, key=lambda p: p[0])
    if descending:
        pts = [p for p in pts]
    if x <= pts[0][0]: return float(pts[0][1])
    if x >= pts[-1][0]: return float(pts[-1][1])
    for (x0,y0),(x1,y1) in zip(pts, pts[1:]):
        if x0 <= x <= x1:
            if x1 == x0: return float(y0)
            return y0 + (y1-y0)*(x-x0)/(x1-x0)
    return float(pts[-1][1])

def agc_to_dbm(agc1, agc2):
    """Input power in dBm from the two AGC gains.

    Two regimes, from the measurements: while the signal is weak the
    tuner runs AGC1 at zero and AGC2 carries the information; once
    AGC2 bottoms out around 148 the LNA gain is being backed off and
    AGC1 carries it instead.
    """
    if agc1 is None or agc2 is None: return None
    if agc1 >= 1:
        return _interp(agc1, AGC1_TO_DBM)
    return _interp(agc2, AGC2_TO_DBM)
