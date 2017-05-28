"""Simulator backends for NeuronUnit models"""

import os
import platform
import time
import re
import copy

from pyneuroml import pynml
from quantities import ms, mV
from neo.core import AnalogSignal


class Backend:
    """Base class for simulator backends that implement simulator-specific
    details of modifying, running, and reading results from the simulation
    """

    def __init__(self, attrs=None):
        self.attrs = {} if attrs is None else attrs

    attrs = None

    # Name of the backend
    backend = None

    #The function (e.g. from pynml) that handles running the simulation
    f = None

    def set_attrs(self, **attrs):
        """Set model attributes, e.g. input resistance of a cell"""
        pass

    def set_run_params(self, **params):
        """Set run-time parameters, e.g. the somatic current to inject"""
        pass

    def load_model(self):
        """Load the model into memory"""
        pass


class jNeuroMLBackend(Backend):
    """Used for simulation with jNeuroML, a reference simulator for NeuroML"""

    backend = 'jNeuroML'

    def set_attrs(self, **attrs):
        self.attrs.update(attrs)
        self.set_lems_attrs(attrs)

    def set_run_params(self, **params):
        self.run_params.update(params)
        self.set_lems_run_params()

    def inject_square_current(self, current):
        self.set_run_params(injected_square_current=current)

    def local_run(self):
        f = pynml.run_lems_with_jneuroml
        result = f(self.lems_file_path, skip_run=self.skip_run,
                    nogui=self.run_params['nogui'],
                    load_saved_data=True, plot=False,
                    verbose=self.run_params['v'])
        return result


class NEURONBackend(Backend):
    """Used for simulation with NEURON, a popular simulator
    http://www.neuron.yale.edu/neuron/
    Units used by NEURON are sometimes different to quantities/neo 
    (note nA versus pA)
    http://neurosimlab.org/ramcd/pyhelp/modelspec/programmatic/mechanisms/mech.html#IClamp
    NEURON's units:
    del -- ms
    dur -- ms
    amp -- nA
    i -- nA
    """

    def __init__(self, attrs=None):
        super(NEURONBackend,self).__init__(attrs)
        self.neuron = None
        self.model_path = None
        from neuron import h
        self.h = h
        self.related_data = {}
        #Should check if MPI parallel neuron is supported and invoked.
        self.h.load_file("stdlib.hoc")
        self.h.load_file("stdgui.hoc")
        #self.h.cvode.active(1)
        #pdb.set_trace()
        #self.h.cvode.active
    
    backend = 'NEURON'

    def reset_h(self, hVariable):
        """Sets the NEURON h variable"""

        self.h = hVariable.h
        self.neuron = hVariable

    def setStopTime(self, stopTime = 1000*ms):
        """Sets the simulation duration
        stopTimeMs: duration in milliseconds
        """

        tstop = stopTime
        tstop.units = ms
        self.h.tstop = float(tstop)

    def setTimeStep(self, integrationTimeStep = 1/128.0 * ms):
        """Sets the simulation itegration fixed time step
        integrationTimeStepMs: time step in milliseconds. 
        Powers of two preferred. Defaults to 1/128.0
        """

        dt = integrationTimeStep
        dt.units = ms

        self.h.dt = self.fixedTimeStep = float(dt)

    def setTolerance(self, tolerance = 0.001):
        """Sets the variable time step integration method absolute tolerance.
        tolerance: absolute tolerance value
        """

        self.h.cvode.atol(tolerance)

    def setIntegrationMethod(self, method = "fixed"):
        """Sets the simulation itegration method
        method: either "fixed" or "variable". Defaults to fixed. 
        cvode is used when "variable" """

        self.h.cvode.active(1 if method == "variable" else 0)


    def get_membrane_potential(self):
        """Must return a neo.core.AnalogSignal.
        And must destroy the hoc vectors that comprise it.
        """
        
        if self.h.cvode.active() == 0:
            dt = float(copy.copy(self.h.dt))
            fixed_signal = copy.copy(self.vVector.to_python())
        else:
            dt = float(copy.copy(self.fixedTimeStep))
            fixed_signal =copy.copy(self.get_variable_step_analog_signal())
            
        self.h.dt = None
        self.fixedTimeStep = None
        return AnalogSignal(fixed_signal, 
                            units = mV, 
                            sampling_period = dt * ms)

    def get_variable_step_analog_signal(self):
        """Converts variable dt array values to fixed 
        dt array by using linear interpolation"""

        # Fixed dt potential
        fPots = []
        fDt = self.fixedTimeStep
        # Variable dt potential
        vPots = self.vVector.to_python()
        # Variable dt times
        vTimes = self.tVector.to_python()
        duration = vTimes[len(vTimes)-1]
        # Fixed and Variable dt times
        fTime = vTime = vTimes[0]
        # Index of variable dt time array
        vIndex = 0
        # Advance the fixed dt position
        while fTime <= duration:

            # If v and f times are exact, no interpolation needed
            if fTime == vTime:
                fPots.append(vPots[vIndex])

            # Interpolate between the two nearest vdt times
            else:

                # Increment vdt time until it surpases the fdt time
                while fTime > vTime and vIndex < len(vTimes):
                    vIndex += 1
                    vTime = vTimes[vIndex]

                # Once surpassed, use the new vdt time and t-1 for interpolation
                vIndexMinus1 = max(0, vIndex-1)
                vTimeMinus1 = vTimes[vIndexMinus1]

                fPot = self.linearInterpolate(vTimeMinus1, vTime, \
                                          vPots[vIndexMinus1], vPots[vIndex], \
                                          fTime)

                fPots.append(fPot)

            # Go to the next fdt time step
            fTime += fDt

        return fPots

    def linearInterpolate(self, tStart, tEnd, vStart, vEnd, tTarget):
        tRange = float(tEnd - tStart)
        tFractionAlong = (tTarget - tStart)/tRange
        vRange = vEnd - vStart
        vTarget = vRange*tFractionAlong + vStart

        return vTarget


    def load_model(self):
        """
        Inputs: NEURONBackend instance object
        Outputs: nothing mutates input object.
        Take a declarative model description, and convert it 
        into an implementation, stored in a pyhoc file.
        import the pyhoc file thus dragging the neuron variables 
        into memory/python name space.
        Since this only happens once outside of the optimization 
        loop its a tolerable performance hit.
        """

        DEFAULTS={}
        DEFAULTS['v']=True
        #Create a pyhoc file using jneuroml to convert from NeuroML to pyhoc.
        #import the contents of the file into the current names space.
        def cond_load():
            from neuronunit.tests.NeuroML2 import LEMS_2007One_nrn
            self.reset_h(LEMS_2007One_nrn.neuron)
            #make sure mechanisms are loaded
            modeldirname=os.path.dirname(self.orig_lems_file_path)
            self.neuron.load_mechanisms(modeldirname)
            #import the default simulation protocol
            from neuronunit.tests.NeuroML2.LEMS_2007One_nrn import NeuronSimulation
            #this next step may be unnecessary: TODO delete it and check.
            self.ns = NeuronSimulation(tstop=1600, dt=0.0025)
            return self

        architecture = platform.machine()
        NEURON_file_path = os.path.join(self.orig_lems_file_path,architecture)
        if os.path.exists(NEURON_file_path):
            self = cond_load()
        else:
            pynml.run_lems_with_jneuroml_neuron(self.orig_lems_file_path,
                              skip_run=False,
                              nogui=False,
                              load_saved_data=False,
                              only_generate_scripts = True,
                              plot=False,
                              show_plot_already=False,
                              exec_in_dir = ".",
                              verbose=DEFAULTS['v'],
                              exit_on_fail=True)


            self=cond_load()
            
        #Although the above approach successfuly instantiates a LEMS/neuroml model in pyhoc
        #the resulting hoc variables for current source and cell name are idiosyncratic (not generic).
        #The resulting idiosyncracies makes it hard not have a hard coded approach make non hard coded, and generalizable code.
        #work around involves predicting the hoc variable names from pyneuroml LEMS file that was used to generate them.
        more_attributes = pynml.read_lems_file(self.orig_lems_file_path)
        #print("Components are %s" % more_attributes.components)
        for i in more_attributes.components:
            #This code strips out simulation parameters from the xml tree also such as duration.
            #Strip out values from something a bit like an xml tree.
            if str('pulseGenerator') in i.type:
                self.current_src_name = i.id
            if str('Cell') in i.type:
                self.cell_name = i.id
        more_attributes = None #force garbage collection of more_attributes, its not needed anymore.
        return self

    def set_run_params(self, **params):
        self.params.update(params)
        for value in params.values():
            h_key, h_value =list(value.items())[0]
            self.h('m_RS_RS_pop[0].%s=%s' % (h_key,h_value))
            self.h('m_%s_%s_pop[0].%s=%s' % \
                   (self.cell_name,self.cell_name,h_key,h_value))
        self.h(' { v_time = new Vector() } ')
        self.h(' { v_time.record(&t) } ')
        self.h(' { v_v_of0 = new Vector() } ')
        self.h(' { v_v_of0.record(&RS_pop[0].v(0.5)) } ')
        self.h(' { v_u_of0 = new Vector() } ')
        self.h(' { v_u_of0.record(&m_RS_RS_pop[0].u) } ')

    def inject_square_current(self,current):
        '''
        Inputs: current : a dictionary
         like:
        {'amplitude':-10.0*pq.pA,
         'delay':100*pq.ms,
         'duration':500*pq.ms}}
        where 'pq' is the quantities package
        '''

        c = copy.copy(current)
        if 'injected_square_current' in c.keys():
            c = current['injected_square_current']

        c['delay'] = re.sub('\ ms$', '', str(c['delay']))
        c['duration'] = re.sub('\ ms$', '', str(c['duration']))
        c['amplitude'] = re.sub('\ pA$', '', str(c['amplitude']))
        #Todo want to convert from nano to pico amps using quantities.
        amps=float(c['amplitude'])/1000.0 #This is the right scale.
        prefix = 'explicitInput_%s%s_pop0.' % (self.current_src_name,self.cell_name)
        self.h(prefix+'amplitude=%s'%amps)
        self.h(prefix+'duration=%s'%c['duration'])
        self.h(prefix+'delay=%s'%c['delay'])
        self.local_run()

    def local_run(self):
        #sim_start = time.time()
        #self.h.tstop=1600#))#TODO find a way to make duration changeable.
        #print(self.h.cvode.active())
        self.h('run()')
        #sim_end = time.time()
        #sim_time = sim_end - sim_start
        #print("Finished NEURON simulation in %f seconds (%f mins)..."%(sim_time, sim_time/60.0))
        self.results={}
        # Convert to Python list for speed, variable has dim: voltage
        self.results['vm'] = [float(x/1000.0) for x in copy.copy(self.neuron.h.v_v_of0.to_python())]  
        #self.neuron.h.v_v_of0 = None # Convert to Python list for speed, variable has dim: voltage
        # Convert to Python list for speed, variable has dim: voltage
        self.results['t'] = [float(x) for x in copy.copy(self.neuron.h.v_time.to_python())]
        #self.neuron.h.v_time = None
        if 'run_number' in self.results.keys():
            self.results['run_number']=self.results['run_number']+1
        else:
            self.results['run_number']=1
        return self.results
