import numpy as np
from cereal import log
from common.filter_simple import FirstOrderFilter
from common.numpy_fast import interp, clip
from common.realtime import DT_MDL
from common.realtime import sec_since_boot
from selfdrive.config import Conversions as CV
from selfdrive.hardware import EON, TICI
from selfdrive.swaglog import cloudlog
from enum import Enum

LaneTraffic = log.LateralPlan.LaneTraffic
Desire = log.LateralPlan.Desire

TRAJECTORY_SIZE = 33
LANE_DEPARTURE_THRESHOLD = 0.1

def min_max_mean(lst):
  mi, ma, me = 0,0,0
  for i in lst:
    if i < mi:
      mi = i
    if i > ma:
      ma = i
    me += i
  if len(lst):
    me /= len(lst)
  return mi,ma,me

# camera offset is meters from center car to camera
if EON:
  CAMERA_OFFSET = 0.06
  PATH_OFFSET = 0.0
elif TICI:
  CAMERA_OFFSET = -0.04
  PATH_OFFSET = -0.04
else:
  CAMERA_OFFSET = 0.0
  PATH_OFFSET = 0.0

LANE_WIDTH_DEFAULT = 3.7

class AUTO_AUTO_LANE_MODE:
  ENGAGE = 1
  NO_CHANGE = 0
  DISENGAGE = -1

class LANE_TRAFFIC:
  NONE = 0
  ONCOMING = 1
  ONGOING = 2
  STOPPED = 3
  
  def to_cereal(lt):
    if lt == LANE_TRAFFIC.NONE:
      return LaneTraffic.none
    elif lt == LANE_TRAFFIC.ONCOMING:
      return LaneTraffic.oncoming
    elif lt == LANE_TRAFFIC.ONGOING:
      return LaneTraffic.ongoing
    else:
      return LaneTraffic.stopped


def lead_between_lines(lll, rll, lead):
  return interp(lead.dRel, lll.x, lll.y) < -lead.yRel < interp(lead.dRel, rll.x, rll.y)

def lead_close_to_line(ll, lead, offset, is_left):
  if is_left:
    return interp(lead.dRel, ll.x, ll.y) - offset < -lead.yRel < interp(lead.dRel, ll.x, ll.y)
  else:
    return interp(lead.dRel, ll.x, ll.y) < -lead.yRel < interp(lead.dRel, ll.x, ll.y) + offset

class LaneOffset: 
  OFFSET = 0.11 # [unitless] offset of the left/right positions as factor of current lane width
  AUTO_OFFSET_FACTOR = 0.8 # 20% less offset when auto mode
  OFFSET_MAX = 0.7 # [m]
  DUR = 2.0 # [s] time it takes to switch lane positions
  STEP = OFFSET / DUR * DT_MDL * LANE_WIDTH_DEFAULT
  
  DUR_SLOW = 8.0 # [s] same, but slower for when position change is caused by auto lane offset
  STEP_SLOW = OFFSET / DUR_SLOW * DT_MDL * LANE_WIDTH_DEFAULT
  
  AUTO_MIN_SHOULDER_WIDTH_FACTOR = 0.05 # [unitless] shoulder width must be as this this factor of lane width
  AUTO_MAX_LANE_WIDTH_FACTOR = 0.6 # [unitless] adjacent lanes whose width is narrower than this factor of lane width are considered a shoulder
  
  AUTO_MAX_PRED_LAT_ACCEL = 0.9 # [m/s^2] more than this predicted amount of lateral accel causes instant resuming of center position
  AUTO_MAX_CUR_LAT_ACCEL = 0.7 # same but for instantaneous vehicle lateral accel
  
  AUTO_MIN_LANELINE_PROB = 0.3
  AUTO_MIN_ADJACENT_LANELINE_PROB = 0.15
  
  AUTO_LANE_STATE_MIN_TIME = 4.0 # [s] amount of time the lane state must stay the same before it can be acted upon
  
  AUTO_ENABLE_ROAD_TYPES = {0, 10, 20, 30} # freeway and state highways (see highway ranks in /Users/haiiro/NoSync/optw/openpilot/selfdrive/mapd/lib/WayRelation.py)
  AUTO_ENABLE_ROAD_TYPE_MIN_SPEED = 40. * CV.MPH_TO_MS
  AUTO_ENABLE_TRAFFIC_MIN_SPEED = 10. * CV.MPH_TO_MS
  
  AUTO_TRAFFIC_TIMEOUT = 30. # [s] amount of time auto lane position will be kept after last car is seen
  AUTO_TRAFFIC_MIN_TIME = 1.1 # [s] time an oncoming/ongoing car needs to be observed before it will be taken to indicate traffic
  AUTO_TRAFFIC_MIN_SPEED = 9. # [m/s] need to go faster than this to be considered moving
  AUTO_CUTOFF_STEER_ANGLE = 110. # [degrees] auto lane position reset traffic monitoring after this
  TRAFFIC_NEW_DETECT_STEER_CUTOFF = 9. # [degrees] instantaneous traffic isn't detected if steer angle greater than this
  AUTO_TRAFFIC_MIN_DIST_LANE_WIDTH_FACTOR = 1.25 # [unitless] factor of current lane width used to check against adjacent lead distance
  AUTO_TRAFFIC_STOPPED_MIN_DIST_LANE_WIDTH_FACTOR = 0.9 # [unitless] factor of current lane width used to check against adjacent lead distance, but for stopped objects
  
  def __init__(self, mass=0.):
    self.offset = 0.
    self.lane_pos = 0.
    self.offset_scale = interp(float(mass), [1607., 2729.], [1., 0.7]) # scales down offset based on vehicle width (assumed to go as mass)
    self._auto_is_active = False
    self._auto_was_enabled_by_road_type = False
    self._road_type = 100 # unknown
    self._lane_pos_auto = 0. # 0., 1., -1. = center, left, right
    self._lane_probs = np.zeros(4) # [left adjacent, left, right, right adjacent]
    self._road_edge_probs = np.zeros(2) # [left, right]
    self._lane_width_mean_left_adjacent = 0.
    self._lane_width_mean_right_adjacent = 0.
    self._shoulder_width_mean_left = 0.
    self._shoulder_width_mean_right = 0.
    self._cs = None
    self._long_plan = None
    self._lat_plan = None
    self._lat_accel_cur = 0.
    self._lat_accel_pred = 0.
    self._lat_curvature_cur = 0.
    self._lat_curvature_pred = 0.
    self._lane_state_changed_last_t = 0.
    self._road_type_last = -1
    self._auto_auto_enabled = False
    self._v_ego_last = 0.
    
    self._left_traffic = LANE_TRAFFIC.NONE
    self._right_traffic = LANE_TRAFFIC.NONE
    self._left_traffic_last = LANE_TRAFFIC.NONE
    self._right_traffic_last = LANE_TRAFFIC.NONE
    self._left_traffic_temp = LANE_TRAFFIC.NONE
    self._right_traffic_temp = LANE_TRAFFIC.NONE
    self._right_traffic_last_seen_t = 0.
    self._left_traffic_last_seen_t = 0.
    self._left_traffic_temp_t = 0.
    self._right_traffic_temp_t = 0.
    self._lprob_last = 0.
    self._rprob_last = 0.
    
    self._auto_auto_lane_position_action = AUTO_AUTO_LANE_MODE.NO_CHANGE
  
  
  def do_auto_enable_traffic(self, ret, v_ego):
    if ret != AUTO_AUTO_LANE_MODE.ENGAGE and (self._left_traffic_last != self._left_traffic or self._right_traffic != self._right_traffic_last \
      or (v_ego >= self.AUTO_ENABLE_TRAFFIC_MIN_SPEED and self._v_ego_last < self.AUTO_ENABLE_TRAFFIC_MIN_SPEED) \
      or (v_ego < self.AUTO_ENABLE_TRAFFIC_MIN_SPEED and self._v_ego_last >= self.AUTO_ENABLE_TRAFFIC_MIN_SPEED) \
      or (self._lane_probs[1] >= self.AUTO_MIN_LANELINE_PROB and self._lprob_last <= self.AUTO_MIN_LANELINE_PROB) \
      or (self._lane_probs[1] < self.AUTO_MIN_LANELINE_PROB and self._lprob_last >= self.AUTO_MIN_LANELINE_PROB) \
      or (self._lane_probs[2] >= self.AUTO_MIN_LANELINE_PROB and self._rprob_last <= self.AUTO_MIN_LANELINE_PROB) \
      or (self._lane_probs[2] < self.AUTO_MIN_LANELINE_PROB and self._rprob_last >= self.AUTO_MIN_LANELINE_PROB)):  
      if not self._auto_is_active and v_ego >= self.AUTO_ENABLE_TRAFFIC_MIN_SPEED \
        and (self._left_traffic != LANE_TRAFFIC.NONE or self._right_traffic != LANE_TRAFFIC.NONE) \
        and self._lane_probs[1] > self.AUTO_MIN_LANELINE_PROB and self._lane_probs[2] > self.AUTO_MIN_LANELINE_PROB:
          ret = AUTO_AUTO_LANE_MODE.ENGAGE
          self._auto_auto_enabled = True
      elif self._auto_is_active and self._auto_auto_enabled \
        and (self._left_traffic == LANE_TRAFFIC.NONE and self._right_traffic == LANE_TRAFFIC.NONE \
          or v_ego < self.AUTO_ENABLE_TRAFFIC_MIN_SPEED \
          or (self._lane_probs[1] < self.AUTO_MIN_LANELINE_PROB and self._lane_probs[2] < self.AUTO_MIN_LANELINE_PROB)):
            ret = AUTO_AUTO_LANE_MODE.DISENGAGE
            self._auto_auto_enabled = False
    return ret

  def do_auto_enable_lanelines(self, ret, road_type, v_ego):
    if road_type != self._road_type_last or ret == AUTO_AUTO_LANE_MODE.DISENGAGE \
        or (v_ego >= self.AUTO_ENABLE_ROAD_TYPE_MIN_SPEED and self._v_ego_last < self.AUTO_ENABLE_ROAD_TYPE_MIN_SPEED) \
        or (v_ego < self.AUTO_ENABLE_ROAD_TYPE_MIN_SPEED and self._v_ego_last >= self.AUTO_ENABLE_ROAD_TYPE_MIN_SPEED):
      if (road_type in self.AUTO_ENABLE_ROAD_TYPES and v_ego >= self.AUTO_ENABLE_ROAD_TYPE_MIN_SPEED and not self._auto_is_active):
        ret = AUTO_AUTO_LANE_MODE.NO_CHANGE if ret == AUTO_AUTO_LANE_MODE.DISENGAGE else AUTO_AUTO_LANE_MODE.ENGAGE
        self._auto_auto_enabled = True
      elif self._auto_is_active and self._auto_auto_enabled \
        and (road_type not in self.AUTO_ENABLE_ROAD_TYPES or v_ego < self.AUTO_ENABLE_ROAD_TYPE_MIN_SPEED) :
          ret = AUTO_AUTO_LANE_MODE.DISENGAGE
          self._auto_auto_enabled = False
    return ret
            
  def do_auto_enable(self, road_type):
    ret = AUTO_AUTO_LANE_MODE.NO_CHANGE
    v_ego = self._cs.vEgo if self._cs is not None else 0.
    
    ret = self.do_auto_enable_traffic(ret, v_ego)
    if ret != AUTO_AUTO_LANE_MODE.ENGAGE:
      ret = self.do_auto_enable_lanelines(ret, road_type, v_ego)

    self._auto_auto_lane_position_action = ret
    self._lprob_last = self._lane_probs[1]
    self._rprob_last = self._lane_probs[2]
    self._left_traffic_last = self._left_traffic
    self._right_traffic_last = self._right_traffic
    self._road_type_last = road_type
    self._v_ego_last = v_ego
    
    return ret
  
  def update_lane_info(self, md, v_ego, lane_width):
    self._lane_width_mean_left_adjacent = 0.
    self._lane_width_mean_right_adjacent = 0.
    self._shoulder_width_mean_left = 0.
    self._shoulder_width_mean_right = 0.    
    self._lane_probs = np.zeros(4) # [left adjacent, left, right, right adjacent]
    self._road_edge_probs = np.zeros(2) # [left, right]
    if len(md.laneLines) == 4 and len(md.laneLines[0].t) == TRAJECTORY_SIZE \
        and len(md.roadEdges) >= 2 and len(md.roadEdges[0].t) == TRAJECTORY_SIZE:
      
      # use mean lane widths so that it can start recentering in response to
      # upcoming bad conditions.
      
      # get road edge "probabilities"
      self._road_edge_probs = [ interp(md.roadEdgeStds[i], [.3, 1.], [1.0, 0.0]) for i in range(2) ]
            
      # get laneline probabilities
      for i in range(3): 
        l_prob, r_prob = md.laneLineProbs[i], md.laneLineProbs[i+1]
        if ((i == 0 and l_prob > self.AUTO_MIN_ADJACENT_LANELINE_PROB) or l_prob > self.AUTO_MIN_LANELINE_PROB) \
            and ((i == 2 and r_prob > self.AUTO_MIN_ADJACENT_LANELINE_PROB) or r_prob > self.AUTO_MIN_LANELINE_PROB):
          # Reduce reliance on uncertain lanelines (less stringent for adjacent lanelines)
          rll_y, lll_y = np.array(md.laneLines[i+1].y), np.array(md.laneLines[i].y)
          
          if i == 0: # set left adjacent 
            self._lane_probs[0] = l_prob
            self._lane_width_mean_left_adjacent = np.mean(rll_y - lll_y)
            if self._road_edge_probs[0] > 0.:
              self._shoulder_width_mean_left = np.mean(lll_y - np.array(md.roadEdges[0].y))
          elif i == 1: # set center
            self._lane_probs[1] = l_prob
            self._lane_probs[2] = r_prob
          else: # set right 
            self._lane_probs[3] = r_prob
            self._lane_width_mean_right_adjacent = np.mean(rll_y - lll_y)
            if self._road_edge_probs[1] > 0.:
              self._shoulder_width_mean_right = np.mean(np.array(md.roadEdges[1].y) - rll_y)

      
      # see if adjacent lane can be treated as shoulder because it's too narrow
      if self._lane_width_mean_left_adjacent < self.AUTO_MAX_LANE_WIDTH_FACTOR * lane_width:
        self._shoulder_width_mean_left += self._lane_width_mean_left_adjacent
        self._lane_width_mean_left_adjacent = 0.
      if self._lane_width_mean_right_adjacent < self.AUTO_MAX_LANE_WIDTH_FACTOR * lane_width:
        self._shoulder_width_mean_right += self._lane_width_mean_right_adjacent
        self._lane_width_mean_right_adjacent = 0.
    return
  
  def update_traffic_info(self, rs, lane_width, md):
    left_traffic = LANE_TRAFFIC.NONE
    right_traffic = LANE_TRAFFIC.NONE
    if self._cs is None or abs(self._cs.steeringAngleDeg) > self.TRAFFIC_NEW_DETECT_STEER_CUTOFF:
      return
    
    leads = rs.leadsLeft
    if len(leads) > 0:
      check_lane_width = lane_width * self.AUTO_TRAFFIC_MIN_DIST_LANE_WIDTH_FACTOR
      if md.laneLineProbs[0] >= self.AUTO_MIN_ADJACENT_LANELINE_PROB and md.laneLineProbs[1] >= self.AUTO_MIN_LANELINE_PROB \
        and md.laneLines[1].y[0] > -2.2:
          lv = [l.vLeadK for l in leads if lead_between_lines(md.laneLines[0], md.laneLines[1], l)]
      elif md.laneLineProbs[1] >= self.AUTO_MIN_LANELINE_PROB and md.laneLines[1].y[0] > -2.:
        lv = [l.vLeadK for l in leads if lead_close_to_line(md.laneLines[1], l, lane_width, True)]
      else:
        lv = [l.vLeadK for l in leads if abs(l.dPath) < check_lane_width]
        
      if len(lv) > 0:
        min_v, max_v, mean_v = min_max_mean(lv)
        check_v = min_v if abs(min_v - mean_v) < abs(max_v - mean_v) else max_v
        if check_v > self.AUTO_TRAFFIC_MIN_SPEED and self._lane_width_mean_left_adjacent > 0.:
          left_traffic = LANE_TRAFFIC.ONGOING
        elif check_v < -self.AUTO_TRAFFIC_MIN_SPEED:
          left_traffic = LANE_TRAFFIC.ONCOMING
        else:
          check_lane_width = lane_width * self.AUTO_TRAFFIC_STOPPED_MIN_DIST_LANE_WIDTH_FACTOR
          lv = [l.vLeadK for l in rs.leadsLeft if abs(l.dPath) < check_lane_width]
          if len(lv) > 0:
            left_traffic = LANE_TRAFFIC.STOPPED
    
    leads = rs.leadsRight
    if len(leads) > 0:
      check_lane_width = lane_width * self.AUTO_TRAFFIC_MIN_DIST_LANE_WIDTH_FACTOR
      if md.laneLineProbs[3] >= self.AUTO_MIN_ADJACENT_LANELINE_PROB and md.laneLineProbs[2] >= self.AUTO_MIN_LANELINE_PROB \
        and md.laneLines[2].y[0] < 2.2:
          lv = [l.vLeadK for l in leads if lead_between_lines(md.laneLines[2], md.laneLines[3], l)]
      elif md.laneLineProbs[2] >= self.AUTO_MIN_LANELINE_PROB and md.laneLines[2].y[0] < 2.:
        lv = [l.vLeadK for l in leads if lead_close_to_line(md.laneLines[2], l, lane_width, False)]
      else:
        lv = [l.vLeadK for l in leads if abs(l.dPath) < check_lane_width]
      
      if len(lv) > 0:
        min_v, max_v, mean_v = min_max_mean(lv)
        check_v = min_v if abs(min_v - mean_v) < abs(max_v - mean_v) else max_v
        if check_v > self.AUTO_TRAFFIC_MIN_SPEED and self._lane_width_mean_right_adjacent > 0.:
          right_traffic = LANE_TRAFFIC.ONGOING
        elif check_v < -self.AUTO_TRAFFIC_MIN_SPEED:
          right_traffic = LANE_TRAFFIC.ONCOMING
        else:
          check_lane_width = lane_width * self.AUTO_TRAFFIC_STOPPED_MIN_DIST_LANE_WIDTH_FACTOR
          lv = [l.vLeadK for l in rs.leadsRight if abs(l.dPath) < check_lane_width]
          if len(lv) > 0:
            right_traffic = LANE_TRAFFIC.STOPPED
    
    if left_traffic != LANE_TRAFFIC.NONE:
      if self._left_traffic_temp != left_traffic:
        self._left_traffic_temp_t = self._t
      elif self._t - self._left_traffic_temp_t >= self.AUTO_TRAFFIC_MIN_TIME:
        self._left_traffic_last_seen_t = self._t
        self._left_traffic = left_traffic
    elif self._t - self._left_traffic_last_seen_t > self.AUTO_TRAFFIC_TIMEOUT:
      self._left_traffic = left_traffic
    
    self._left_traffic_temp = left_traffic
      
    if right_traffic != LANE_TRAFFIC.NONE:
      if self._right_traffic_temp != right_traffic:
        self._right_traffic_temp_t = self._t
      elif self._t - self._right_traffic_temp_t >= self.AUTO_TRAFFIC_MIN_TIME:
        self._right_traffic_last_seen_t = self._t
        self._right_traffic = right_traffic
    elif self._t - self._right_traffic_last_seen_t > self.AUTO_TRAFFIC_TIMEOUT:
      self._right_traffic = right_traffic
    
    self._right_traffic_temp = right_traffic
      
    return
  
  def update_lane_pos_auto(self, lane_width):
    lane_pos_auto = 0.
    timeout_override = False
    
    if self._left_traffic != LANE_TRAFFIC.NONE \
        and self._right_traffic == LANE_TRAFFIC.NONE:
      lane_pos_auto = -1.
      timeout_override = True
    elif self._right_traffic != LANE_TRAFFIC.NONE \
        and self._left_traffic == LANE_TRAFFIC.NONE:
      lane_pos_auto = 1.
      timeout_override = True
    elif self._lane_probs[1] > self.AUTO_MIN_LANELINE_PROB \
        and self._lane_probs[2] > self.AUTO_MIN_LANELINE_PROB:
      if (self._lane_width_mean_left_adjacent > 0. and self._lane_width_mean_right_adjacent > 0.) \
          or (self._lane_width_mean_left_adjacent == 0. and self._lane_width_mean_right_adjacent == 0.):
        lane_pos_auto = 0.
      elif self._lane_width_mean_left_adjacent > 0. and self._shoulder_width_mean_right >= self.AUTO_MIN_SHOULDER_WIDTH_FACTOR * lane_width:
        lane_pos_auto = -1.
      elif self._lane_width_mean_right_adjacent > 0. and self._shoulder_width_mean_left >= self.AUTO_MIN_SHOULDER_WIDTH_FACTOR * lane_width:
        lane_pos_auto = 1.
    if lane_pos_auto != 0. \
        and ((self._lat_accel_cur >= self.AUTO_MAX_CUR_LAT_ACCEL \
          and np.sign(self._lat_curvature_cur) == np.sign(lane_pos_auto)) \
        or (self._lat_accel_pred >= self.AUTO_MAX_PRED_LAT_ACCEL \
          and np.sign(self._lat_curvature_pred) == np.sign(lane_pos_auto))):
      lane_pos_auto = 0.
    
    if lane_pos_auto != self._lane_pos_auto:
      self._lane_state_changed_last_t = self._t
      if timeout_override:
        self._lane_state_changed_last_t -= self.AUTO_LANE_STATE_MIN_TIME + 1.
    
    self._lane_pos_auto = lane_pos_auto
    return lane_pos_auto
    
  def update(self, lane_pos=0., lane_width=LANE_WIDTH_DEFAULT, auto_active=False, md=None, sm=None): # 0., 1., -1. = center, left, right
    self._t = sec_since_boot()
    
    if sm is not None:
      if sm.valid.get('carState', False):
        self._cs = sm['carState']
      if sm.valid.get('longitudinalPlan', False):
        self._long_plan = sm['longitudinalPlan']
      if sm.valid.get('lateralPlan', False):
        self._lat_plan = sm['lateralPlan']
    
    if md is not None:
      if self._cs is not None:
        self.update_lane_info(md, self._cs.vEgo, lane_width)
        lane_depart = False
        if self._lat_plan is not None:
          right_lane_visible = self._lat_plan.rProb > 0.3
          left_lane_visible = self._lat_plan.lProb > 0.3
          l_lane_change_prob = md.meta.desirePrediction[Desire.laneChangeLeft - 1]
          r_lane_change_prob = md.meta.desirePrediction[Desire.laneChangeRight - 1]
          l_lane_close = left_lane_visible and (md.laneLines[1].y[0] > -(0.5 + self.offset))
          r_lane_close = right_lane_visible and (md.laneLines[2].y[0] < (0.5 - self.offset))
          l_lane_depart = l_lane_change_prob > LANE_DEPARTURE_THRESHOLD and l_lane_close
          r_lane_depart = r_lane_change_prob > LANE_DEPARTURE_THRESHOLD and r_lane_close
          lane_depart = l_lane_depart or r_lane_depart
          
        if abs(self._cs.steeringAngleDeg) > self.AUTO_CUTOFF_STEER_ANGLE or lane_depart:
          self._left_traffic_last_seen_t -= self.AUTO_TRAFFIC_TIMEOUT + 1
          self._right_traffic_last_seen_t -= self.AUTO_TRAFFIC_TIMEOUT + 1
          self._right_traffic = LANE_TRAFFIC.NONE
          self._left_traffic = LANE_TRAFFIC.NONE
          
        if self._right_traffic == LANE_TRAFFIC.ONGOING and self._lane_width_mean_right_adjacent == 0.:
          self._right_traffic = LANE_TRAFFIC.NONE
          self._right_traffic_last_seen_t -= self.AUTO_TRAFFIC_TIMEOUT + 1
          
        if self._left_traffic == LANE_TRAFFIC.ONGOING and self._lane_width_mean_left_adjacent == 0.:
          self._left_traffic = LANE_TRAFFIC.NONE
          self._left_traffic_last_seen_t -= self.AUTO_TRAFFIC_TIMEOUT + 1
          
        if self._lat_plan.lProb < 0.2 and self._left_traffic == LANE_TRAFFIC.NONE \
            and self._t - self._lane_state_changed_last_t < self.AUTO_LANE_STATE_MIN_TIME:
          self._left_traffic_last_seen_t -= self.AUTO_TRAFFIC_TIMEOUT + 1
          
        if self._lat_plan.rProb < 0.2 and self._right_traffic == LANE_TRAFFIC.NONE \
            and self._t - self._lane_state_changed_last_t < self.AUTO_LANE_STATE_MIN_TIME:
          self._right_traffic_last_seen_t -= self.AUTO_TRAFFIC_TIMEOUT + 1
          
        self.update_traffic_info(sm['radarState'], lane_width, md)
        self.update_lane_pos_auto(lane_width)
      if self._long_plan is not None:
        self._lat_accel_cur = self._long_plan.visionCurrentLateralAcceleration
        self._lat_accel_pred = self._long_plan.visionMaxPredictedLateralAcceleration
        self._lat_curvature_pred = self._long_plan.visionMaxPredictedCurvature
      if self._lat_plan is not None and len(self._lat_plan.curvatures) > 0:
        self._lat_curvature_cur = self._lat_plan.curvatures[0]
      self._auto_is_active = auto_active
    else:
      self._auto_is_active = False
      
    do_slow = self._auto_is_active # and self._lane_pos_auto != 0.
    if self._auto_is_active:
      if self._t - self._lane_state_changed_last_t > self.AUTO_LANE_STATE_MIN_TIME:
        self.lane_pos = self._lane_pos_auto
      else:
        self.lane_pos = 0.
    elif self._auto_auto_lane_position_action == AUTO_AUTO_LANE_MODE.DISENGAGE:
      self.lane_pos = 0.
    else:
      self.lane_pos = lane_pos
    offset = self.OFFSET * self.offset_scale * self.lane_pos * lane_width * (self.AUTO_OFFSET_FACTOR if self._auto_is_active else 1.)
    if offset > self.offset:
      self.offset = min(offset, self.offset + (self.STEP_SLOW if do_slow else self.STEP))
    elif offset < self.offset:
      self.offset = max(offset, self.offset - (self.STEP_SLOW if do_slow else self.STEP))
    return clip(self.offset, -self.OFFSET_MAX, self.OFFSET_MAX)

class LanePlanner:
  def __init__(self, wide_camera=False, mass=0.):
    self.ll_t = np.zeros((TRAJECTORY_SIZE,))
    self.ll_x = np.zeros((TRAJECTORY_SIZE,))
    self.lll_y = np.zeros((TRAJECTORY_SIZE,))
    self.rll_y = np.zeros((TRAJECTORY_SIZE,))
    self.lane_width_estimate = FirstOrderFilter(LANE_WIDTH_DEFAULT, 9.95, DT_MDL)
    self.lane_width_certainty = FirstOrderFilter(1.0, 0.95, DT_MDL)
    self.lane_width = LANE_WIDTH_DEFAULT
    self.lane_offset = LaneOffset(mass)

    self.lll_prob = 0.
    self.rll_prob = 0.
    self.d_prob = 0.

    self.lll_std = 0.
    self.rll_std = 0.

    self.l_lane_change_prob = 0.
    self.r_lane_change_prob = 0.

    self.camera_offset = -CAMERA_OFFSET if wide_camera else CAMERA_OFFSET
    self.path_offset = -PATH_OFFSET if wide_camera else PATH_OFFSET

  def parse_model(self, md, lane_pos = 0., sm=None, auto_lane_pos_active=False):
    offset = self.lane_offset.update(lane_pos, self.lane_width, auto_active=auto_lane_pos_active, md=md, sm=sm)
    if len(md.laneLines) == 4 and len(md.laneLines[0].t) == TRAJECTORY_SIZE:
      self.ll_t = (np.array(md.laneLines[1].t) + np.array(md.laneLines[2].t))/2
      # left and right ll x is the same
      self.ll_x = md.laneLines[1].x
      # only offset left and right lane lines; offsetting path does not make sense
      self.lll_y = np.array(md.laneLines[1].y) - self.camera_offset - offset
      self.rll_y = np.array(md.laneLines[2].y) - self.camera_offset - offset
      self.lll_prob = md.laneLineProbs[1]
      self.rll_prob = md.laneLineProbs[2]
      self.lll_std = md.laneLineStds[1]
      self.rll_std = md.laneLineStds[2]

    if len(md.meta.desireState):
      self.l_lane_change_prob = md.meta.desireState[log.LateralPlan.Desire.laneChangeLeft]
      self.r_lane_change_prob = md.meta.desireState[log.LateralPlan.Desire.laneChangeRight]

  def get_d_path(self, v_ego, path_t, path_xyz):
    # Reduce reliance on lanelines that are too far apart or
    # will be in a few seconds
    path_xyz[:, 1] -= self.path_offset
    l_prob, r_prob = self.lll_prob, self.rll_prob
    width_pts = self.rll_y - self.lll_y
    prob_mods = []
    for t_check in [0.0, 1.5, 3.0]:
      width_at_t = interp(t_check * (v_ego + 7), self.ll_x, width_pts)
      prob_mods.append(interp(width_at_t, [4.0, 5.0], [1.0, 0.0]))
    mod = min(prob_mods)
    l_prob *= mod
    r_prob *= mod

    # Reduce reliance on uncertain lanelines
    l_std_mod = interp(self.lll_std, [.15, .3], [1.0, 0.0])
    r_std_mod = interp(self.rll_std, [.15, .3], [1.0, 0.0])
    l_prob *= l_std_mod
    r_prob *= r_std_mod

    # Find current lanewidth
    self.lane_width_certainty.update(l_prob * r_prob)
    current_lane_width = abs(self.rll_y[0] - self.lll_y[0])
    self.lane_width_estimate.update(current_lane_width)
    speed_lane_width = interp(v_ego, [0., 31.], [2.5, 3.5])
    self.lane_width = self.lane_width_certainty.x * self.lane_width_estimate.x + \
                      (1 - self.lane_width_certainty.x) * speed_lane_width

    clipped_lane_width = min(4.0, self.lane_width)
    path_from_left_lane = self.lll_y + clipped_lane_width / 2.0
    path_from_right_lane = self.rll_y - clipped_lane_width / 2.0

    self.d_prob = l_prob + r_prob - l_prob * r_prob
    lane_path_y = (l_prob * path_from_left_lane + r_prob * path_from_right_lane) / (l_prob + r_prob + 0.0001)
    safe_idxs = np.isfinite(self.ll_t)
    if safe_idxs[0]:
      lane_path_y_interp = np.interp(path_t, self.ll_t[safe_idxs], lane_path_y[safe_idxs])
      path_xyz[:,1] = self.d_prob * lane_path_y_interp + (1.0 - self.d_prob) * path_xyz[:,1]
    else:
      cloudlog.warning("Lateral mpc - NaNs in laneline times, ignoring")
    return path_xyz
