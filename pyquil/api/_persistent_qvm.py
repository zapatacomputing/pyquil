##############################################################################
# Copyright 2019 Rigetti Computing
#
#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.
##############################################################################
from types import TracebackType
from typing import Any, Dict, List, Optional, Type

import numpy as np

from pyquil.api._base_connection import (ForestConnection, QVMAllocationMethod, QVMSimulationMethod,
                                         validate_allocation_method, validate_num_qubits,
                                         validate_noise_probabilities, validate_job_sub_request,
                                         validate_job_token, validate_persistent_qvm_token,
                                         validate_simulation_method, qvm_ng_run_program_payload)
from pyquil.api._error_reporting import _record_call
from pyquil.api._qvm import QVMNotRunning, QVMVersionMismatch
from pyquil.quil import Program, get_classical_addresses_from_program


def check_qvm_ng_version(version: str) -> None:
    """
    Verify that there is no mismatch between pyquil and QVM versions.

    :param version: The version of the QVM
    """
    major, minor, patch = map(int, version.split("."))
    if major == 1 and minor < 11:
        raise QVMVersionMismatch("Must use QVM >= 1.11.0 with the PersistentQVM, but you "
                                 f"have QVM {version}.")


@_record_call
def get_qvm_memory_estimate(num_qubits: int,
                            connection: Optional[ForestConnection] = None,
                            simulation_method: QVMSimulationMethod = QVMSimulationMethod.PURE_STATE,
                            allocation_method: QVMAllocationMethod = QVMAllocationMethod.NATIVE,
                            measurement_noise: Optional[List[float]] = None,
                            gate_noise: Optional[List[float]] = None,) -> int:
    """
    Return an estimate of the number of bytes required to store the quantum state of a
    PersistentQVM.

    :param num_qubits: The maximum number of qubits available to this QVM.
    :param connection: An optional :py:class:`ForestConnection` object.  If not specified, the
        default values for URL endpoints will be used, and your API key will be read from
        ~/.pyquil_config.  If you deign to change any of these parameters, pass your own
        :py:class:`ForestConnection` object.
    :param simulation_method: The simulation method to use for this PersistentQVM.  See the enum
        QVMSimulationmethod for valid values.
    :param allocation_method: The allocation method to use for this PersistentQVM.  See the enum
        QVMAllocationmethod for valid values.
    :param measurement_noise: A list of three numbers [Px, Py, Pz] indicating the probability of an
        X, Y, or Z gate getting applied before a measurement.  The default value of None indicates
        no noise.
    :param gate_noise: A list of three numbers [Px, Py, Pz] indicating the probability of an X, Y,
        or Z gate getting applied to each qubit after a gate application or reset.  The default
        value of None indicates no noise.

    :return: the number of bytes
    """
    validate_num_qubits(num_qubits)
    validate_simulation_method(simulation_method)
    validate_allocation_method(allocation_method)
    validate_noise_probabilities(measurement_noise)
    validate_noise_probabilities(gate_noise)

    if connection is None:
        connection = ForestConnection()

    return connection._qvm_ng_qvm_memory_estimate(simulation_method, allocation_method,
                                                  num_qubits, measurement_noise, gate_noise)


class AsyncJob:
    def __init__(self, sub_request: Dict[str, Any], connection: Optional[ForestConnection] = None):
        """
        :param sub_request: a dict representing the JSON payload for the RPC method that will be run
            asynchronously.  At a minimum, this must contain a key "type" specifying the RPC method
            to run, along with keys/values for the RPC parameters expected by the requested RPC
            method.
        :param connection: A connection to the Forest web API.
        """
        validate_job_sub_request(sub_request)

        if connection is None:
            connection = ForestConnection()

        self.connection = connection
        self.connect()
        self.token = self.connection._qvm_ng_create_job(sub_request)

    def __del__(self) -> None:
        self.close()

    def __enter__(self) -> "AsyncJob":
        return self

    # If the return type annotation here is changed to bool, mypy complains:
    #
    #     "bool" is invalid as return type for "__exit__" that always returns False. Use
    #     "typing_extensions.Literal[False]" as the return type or change it to "None". If return
    #     type of "__exit__" implies that it may return True, the context manager may swallow
    #     exceptions
    #
    # Use None for now to placate mypy and avoid a dependency on typing_extensions.
    def __exit__(self,
                 exc_type: Optional[Type[BaseException]],
                 exc_value: Optional[BaseException],
                 traceback: Optional[TracebackType]) -> None:
        self.close()

    def connect(self) -> None:
        try:
            version = self.get_version_info()
            check_qvm_ng_version(version)
        except ConnectionError:
            raise QVMNotRunning(f"No QVM-NG server running at {self.connection.qvm_ng_endpoint}")

    def close(self) -> None:
        if self.connection is not None:
            self.connection._qvm_ng_delete_job(self.token)
            self.connection = None

    @_record_call
    def get_version_info(self) -> str:
        """
        Return version information for the connected QVM.

        :return: String with version information
        """
        return self.connection._qvm_ng_get_version_info()

    @_record_call
    def get_job_info(self) -> Dict[str, Any]:
        """
        Fetch the status of this ``AsyncJob``.

        :return: a dict with the async job's status info
        """
        return self.connection._qvm_ng_job_info(self.token)

    @_record_call
    def get_job_result(self):
        """
        Fetch the result of this ``AsyncJob``.  This call will block waiting for the job to
        complete.

        The return type varies depending on the async job that was run.

        :return: the job results
        """
        return self.connection._qvm_ng_job_result(self.token)


class PersistentQVM:
    """
    Represents a connection to a PersistentQVM.
    """
    @_record_call
    def __init__(self, num_qubits: int,
                 connection: Optional[ForestConnection] = None,
                 simulation_method: QVMSimulationMethod = QVMSimulationMethod.PURE_STATE,
                 allocation_method: QVMAllocationMethod = QVMAllocationMethod.NATIVE,
                 measurement_noise: Optional[List[float]] = None,
                 gate_noise: Optional[List[float]] = None,
                 random_seed: Optional[int] = None) -> None:
        """
        A PersistentQVM that classically emulates the execution of Quil programs.

        :param num_qubits: The maximum number of qubits available to this QVM.
        :param connection: A connection to the Forest web API.
        :param simulation_method: The simulation method to use for this PersistentQVM.
            See the enum QVMSimulationmethod for valid values.
        :param allocation_method: The allocation method to use for this PersistentQVM.
            See the enum QVMAllocationmethod for valid values.
        :param measurement_noise: A list of three numbers [Px, Py, Pz] indicating the probability
            of an X, Y, or Z gate getting applied before a measurement. The default value of
            None indicates no noise.
        :param gate_noise: A list of three numbers [Px, Py, Pz] indicating the probability of an X,
           Y, or Z gate getting applied to each qubit after a gate application or reset. The
           default value of None indicates no noise.
        :param random_seed: A seed for the QVM's random number generators. Either None (for an
            automatically generated seed) or a non-negative integer.
        """
        validate_num_qubits(num_qubits)
        validate_simulation_method(simulation_method)
        validate_allocation_method(allocation_method)
        validate_noise_probabilities(measurement_noise)
        validate_noise_probabilities(gate_noise)

        self.num_qubits = num_qubits
        self.simulation_method = simulation_method
        self.allocation_method = allocation_method
        self.measurement_noise = measurement_noise
        self.gate_noise = gate_noise

        if random_seed is None:
            self.random_seed = None
        elif isinstance(random_seed, int) and random_seed >= 0:
            self.random_seed = random_seed
        else:
            raise TypeError("random_seed should be None or a non-negative int")

        if connection is None:
            connection = ForestConnection()

        self.connection = connection
        self.connect()
        self.token = self.connection._qvm_ng_create_qvm(simulation_method,
                                                        allocation_method,
                                                        num_qubits,
                                                        measurement_noise,
                                                        gate_noise)

    def __del__(self) -> None:
        self.close()

    def __enter__(self) -> "PersistentQVM":
        return self

    # If the return type annotation here is changed to bool, mypy complains:
    #
    #     "bool" is invalid as return type for "__exit__" that always returns False. Use
    #     "typing_extensions.Literal[False]" as the return type or change it to "None". If return
    #     type of "__exit__" implies that it may return True, the context manager may swallow
    #     exceptions
    #
    # Use None for now to placate mypy and avoid a dependency on typing_extensions.
    def __exit__(self,
                 exc_type: Optional[Type[BaseException]],
                 exc_value: Optional[BaseException],
                 traceback: Optional[TracebackType]) -> None:
        self.close()

    def connect(self) -> None:
        try:
            version = self.get_version_info()
            check_qvm_ng_version(version)
        except ConnectionError:
            raise QVMNotRunning(f"No QVM-NG server running at {self.connection.qvm_ng_endpoint}")

    def close(self) -> None:
        if self.connection is not None:
            self.connection._qvm_ng_delete_qvm(self.token)
            self.connection = None

    @_record_call
    def get_version_info(self) -> str:
        """
        Return version information for the connected QVM.

        :return: String with version information
        """
        return self.connection._qvm_ng_get_version_info()

    @_record_call
    def get_qvm_info(self) -> Dict[str, Any]:
        """
        Return configuration information about the PersistentQVM.

        :return: Dict with QVM information
        """
        return self.connection._qvm_ng_qvm_info(self.token)

    @_record_call
    def read_memory(self, classical_addresses) -> Dict[str, np.array]:
        """
        Return the contents of this PersistentQVM's classical memory registers.

        :param classical_addresses: A mapping from memory region names to lists of offsets.
        :return: Dict mapping memory region names to values for the corresponding requested offsets.
        """
        return self.connection._qvm_ng_read_memory(self.token, classical_addresses)

    @_record_call
    def write_memory(self, memory_contents) -> None:
        """
        Write ``memory_contents`` to this PersistentQVM's classical memory registers.

        :param memory_contents: A dictionary specifying the classical memory to overwrite.  The keys
            are the names of memory regions, and the values are either (1) a sequence of (index,
            value) pairs such that each value is stored at the corresponding index in the given
            memory region, or (2) a sequence of values such that the ith item in the sequence will
            be stored at the ith index of the memory region.  For example, ``memory_contents`` of
            ``{"theta": [(0, 1.0), (1, 2.3)]}`` indicates that the value ``1.0`` will be written to
            ``theta[0]`` while the value ``2.3`` is writtent to ``theta[1]``.  Equivalently, the
            caller may instead pass a ``memory_contents`` of ``{"theta": [1.0, 2.3]}`` to acheive
            the same result.
        """
        self.connection._qvm_ng_write_memory(self.token, memory_contents)

    @_record_call
    def resume(self) -> None:
        """
        Resume execution of the PersistentQVM.

        The PersistentQVM must be in the WAITING state due to having executed a Quil WAIT
        instruction.
        """
        self.connection._qvm_ng_resume(self.token)

    @_record_call
    def run_program(self, quil_program: Program) -> Dict[str, np.array]:
        """
        Run quil_program on this PersistentQVM instance, and return the values stored in all of the
        classical registers assigned to by the program.

        :param quil_program: the Quil program to run.

        :return: A Dict mapping classical memory names to values.
        """
        if not isinstance(quil_program, Program):
            raise TypeError(f"quil_program must be a Quil Program. Got {quil_program}.")

        classical_addresses = get_classical_addresses_from_program(quil_program)
        return self.connection._qvm_ng_run_program(quil_program=quil_program,
                                                   qvm_token=self.token,
                                                   simulation_method=None,
                                                   allocation_method=None,
                                                   classical_addresses=classical_addresses,
                                                   measurement_noise=None,
                                                   gate_noise=None,
                                                   random_seed=self.random_seed)

    @_record_call
    def run_program_async(self, quil_program: Program) -> AsyncJob:
        """
        Like ``run_program``, but run the program asynchronously.

        :param quil_program: the Quil program to run.
        :return: an ``AsyncJob`` running the requested ``Program``.
        """
        if not isinstance(quil_program, Program):
            raise TypeError(f"quil_program must be a Quil Program. Got {quil_program}.")

        classical_addresses = get_classical_addresses_from_program(quil_program)
        sub_request = qvm_ng_run_program_payload(quil_program=quil_program,
                                                 qvm_token=self.token,
                                                 simulation_method=None,
                                                 allocation_method=None,
                                                 classical_addresses=classical_addresses,
                                                 measurement_noise=None,
                                                 gate_noise=None,
                                                 random_seed=self.random_seed)
        return AsyncJob(sub_request, connection=self.connection)