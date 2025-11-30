from opentrons.protocol_api.instrument_context import InstrumentContext
from opentrons.protocol_api.labware import Well
from typing import TypedDict, Dict, List, Union, Callable, Optional

#------------ Stock and Core Well Definitions ------------

# Used to store the chemical information and address the wells

class StockWell(TypedDict):
    """Basic well for stock - what and how much."""
    position: Well
    volume: float

SubstanceHistory = Dict[str, Union[str, None, tuple[str, str, float]]]

class CoreWell(TypedDict, total=False):
    """Well for core plates - tracks substance history."""
    position: Well
    volume: float
    substance: SubstanceHistory
    max_volume: float

#------------ JSON Type Definitions ------------

# We strictly define that the content of the arguments 
# passed to the functions must be something serialisable to JSON.

JSONScalar = Union[str, int, float, bool, None]
JSONType = Union[JSONScalar, List["JSONType"], Dict[str, "JSONType"]]

#------------ Context Data Definition ------------

#Information about the device objects that is updated after every action

class SystemState(TypedDict, total=False):
    """Tracks what was the last action performed and on which plate/well."""
    plate: str | None
    well: str | None
    last_action: str | None             # registry key
    timestamp: float                    # when the last action happened

class StaticCtx(TypedDict):
    """Holds all setup information for action functions: amounts, pipettes, state."""
    core_amounts: Dict[str, Dict[str, CoreWell]]
    stock_amounts: Dict[str, List[StockWell]]
    pipettes: Dict[str, InstrumentContext]
    state: SystemState

#------------ Action Function Type Definition ------------

ActionFn = Callable[[StaticCtx, dict[str, JSONType]], bool]

#------------ Config formatting ------------

class PlateContent(TypedDict):
    """Auxiliary per-well content information for plate setup."""
    volume: float
    substance: str

# ---------- Full plate configuration ----------
class PlateInfo(TypedDict, total=False):
    """Full plate configuration information for initialization."""
    type: str                        # name of JSON or labware model
    place: str                       # deck position, e.g. "1"
    max_volume: float                # µL max capacity per well
    offset: Dict[str, float]         # x/y/z adjustments
    content: Dict[str, PlateContent] # optional per-well fill info before the expt

# ---------- Pipette mount configuration ----------
class PipetteInfo(TypedDict):
    """Auxiliary pipette information for initialization."""
    model: str

# ---------- Full base config for Opentrons class ----------
class BaseConfig(TypedDict):
    """Base configuration of hardware for Opentrons initialization."""
    pipettes: Dict[str, PipetteInfo]              # e.g. {"left": {...}, "right": {...}}
    core_plates: Dict[str, PlateInfo]             # user-assigned plates
    stock_plates: Dict[str, PlateInfo]            # virtual source-only plates

# ---------- Full agent config for Agent class ----------
class AgentConfig(TypedDict):
    """What should agent monitor and what to do."""
    trigger: str  # e.g. "totally_not_a_file.json"
    action: str   # e.g. "transfer_liquid"


