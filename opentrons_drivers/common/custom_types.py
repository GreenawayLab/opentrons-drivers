from opentrons.protocol_api.instrument_context import InstrumentContext
from opentrons.protocol_api.labware import Well
from typing import TypedDict, Dict, List, Union, Callable, Optional

#------------ Stock and Core Well Definitions ------------

# Used to store the chemical information and address the wells

class StockWell(TypedDict):
    position: Well
    volume: float

SubstanceHistory = Dict[str, Union[str, None, tuple[str, str, float]]]

class CoreWell(TypedDict, total=False):
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

class StaticCtx(TypedDict):
    core_amounts: Dict[str, Dict[str, CoreWell]]
    stock_amounts: Dict[str, List[StockWell]]
    pipettes: Dict[str, InstrumentContext]

#------------ Action Function Type Definition ------------

ActionFn = Callable[[StaticCtx, dict[str, JSONType]], bool]

#------------ Config formatting ------------

class PlateContent(TypedDict):
    volume: float
    substance: str

# ---------- Full plate configuration ----------
class PlateInfo(TypedDict, total=False):
    type: str                        # name of JSON or labware model
    place: str                       # deck position, e.g. "1"
    max_volume: float                # µL max capacity per well
    offset: Dict[str, float]         # x/y/z adjustments
    content: Dict[str, PlateContent] # optional per-well fill info before the expt

# ---------- Pipette mount configuration ----------
class PipetteInfo(TypedDict):
    model: str

# ---------- Full base config for Opentrons class ----------
class BaseConfig(TypedDict):
    pipettes: Dict[str, PipetteInfo]              # e.g. {"left": {...}, "right": {...}}
    core_plates: Dict[str, PlateInfo]             # user-assigned plates
    stock_plates: Dict[str, PlateInfo]            # virtual source-only plates
    gantry_speed_X: Optional[float]
    gantry_speed_Y: Optional[float]               # for high-precision moves
    gantry_speed_Z: Optional[float]

# ---------- Full agent config for Agent class ----------
class AgentConfig(TypedDict):
    trigger: str  # e.g. "totally_not_a_file.json"
    action: str   # e.g. "transfer_liquid"

class AgentState(TypedDict, total=False):
    plate: str | None
    well: str | None
    last_action: str | None             # registry key
    timestamp: float                    # when the last action happened
