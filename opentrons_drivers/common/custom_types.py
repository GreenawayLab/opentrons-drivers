from opentrons.protocol_api.instrument_context import InstrumentContext
from opentrons.protocol_api.labware import Well
from typing import TypedDict, Dict, List, Union, Callable

#------------ Stock and Core Well Definitions ------------

# Used to refer to the results of robot's configuring

class StockWell(TypedDict):
    position: Well
    volume: float

class CoreWell(TypedDict, total=False):
    position: Well
    volume: float
    substance: Dict[str, Union[str, None]]
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

ActionFn = Callable[[StaticCtx, dict[str, JSONType]]]