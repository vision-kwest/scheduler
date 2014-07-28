import os, logging
from multiprocessing import Manager
import scheduler.util.plumber

def isThreaded(componentName):
    '''
    Determine weather the given component should be started as a system-thread 
    or a system-process.
    
    Parameters:
        componentName - The name of a component
    Returns:
        'True' if the given component should run in a thread or else 'False'.
    '''
    return componentName.startswith('_') and componentName.endswith('_') 

def isFramework(processName):
    '''
    Determine weather the given process was specified in the users original 
    graph file or auto-generated, for some reason, by the framework.
    
    Parameters:
        processName - The name of a process.
    Returns:
        'True' if the given process was auto-generated by the framework
        or else 'False'.
    '''
    return processName.startswith('*') and processName.endswith('*') 

def internalEvent(core, eventType):
    '''
    Sends a message (or event), of the given type, out of the 'events'
    port of a process.  These messages allow internal framework events to 
    propagate through the graph like any other information packet.   
    Note: An event can block the execution of a process, if it has been 
          configured to do so (and must then be unblocked by the receiver
          of the event).
    
    Parameters:
        core - a dictionary of internal framework attributes
        eventType - a string representing a special internal event
    '''
    eventSender = core['name']
    # Construct an event (or notification message).
    event  = {'sender' : eventSender,
              'type'   : eventType}
    # Does this event have framework-blocking powers?
    config = core['getConfig']()
    isEventBlocking = False
    try:
        isEventBlocking = config['blocking'][eventType]
    except TypeError:
        # No config data for this process
        pass
    except KeyError:
        # No blocking info in this config data
        pass
    # If blocking enabled for the given event type attach an object to the out 
    # going message that allows a recipient to unblock this process. 
    if isEventBlocking:
        event['blocker'] = Manager().Event()
        core['setData']('events', event)
        event['blocker'].wait()
    # Blocking in not enabled so just send the event.
    else:
        core['setData']('events', event)
    
def fxn(core, inports, outports, fxn):
    '''
    This is canonical framework functionality for a generic component:
    * handle Pipe file descriptor leak
    * setup API to get and set data on named ports
    * wait for data to arrive on in-ports
    * run component logic
    * wait for EOF on in-ports
    * close all in-ports and out-ports
    
    Parameters:
        core - a dictionary of internal framework attributes
        inports - the named input ports
        outports - the named output ports
        fxn - the component logic 
    '''
    FIRST_CONN = 0
    state      = {}
    # round robin support
    state['set data count'] = {}
    for portName in outports.keys():
        state['set data count'][portName] = [ 0, len(outports[portName]) ]
    # event support
    state['has all inputs'] = False
    # Close un-used end of Pipe connection
    for ports in [inports, outports, core.get('ports', {})]:
        for portName, leakyPipeList in ports.items():
            for leakyPipe in leakyPipeList:
                scheduler.util.plumber.plugLeak(leakyPipe, core['name'])
    # Log that this component has started
    logging.debug('BGIN: {name}'.format(name=core['name']))
    # Create helper functions
    received = set([])
    def checkInputs():
        '''
        Sends a 'ReceivedAllInputs' when data has arrived at all in-ports and
        the process is ready execute its logic.
        '''
        # MAINT: Multi-input components like Merge are not handled properly.
        #        We need to account for data arriving from every connection
        #        not just the first one.   
        if not state['has all inputs'] and received == set(inports.keys()):
            internalEvent(core, 'ReceivedAllInputs')
            state['has all inputs'] = True
    def lenAtFxn(portName, inport=True):
        '''
        Gets the number of components connected to a single port.
        
        Parameters:
            portName - The name of the port to query.
            inport - When 'True', the default, the given port name is assumed
                     to be an input port, otherwise it's checked as an out-port.
                     
        Returns:
            An 'int' representing the number of connections attached to the 
            given port.
        '''
        if inport:
            return len(inports[portName])
        return len(outports[portName])
    def getDataAtFxn(connIndex, inportName, block=True):
        '''
        Get the next information packet that arrives at the given in-port name
        from the one connection (of many) associated with the given connection
        index.  If there is no data at the port, this method blocks until data
        arrives, unless the option poll the port is specified.  In the polling
        case, it is non-blocking and an exception is thrown when there is no 
        data.   
        
        Parameters:
            connIndex - An index representing one, of potentially many, 
                        connections on the given in-port name.
            inportName - The name of the in-port to get data from. 
            block - When 'True', the default, this method blocks until data
                    arrives, on the given in-port name, from the connection 
                    associated with the given connection index. If 'False',
                    the method returns immediately with data or it throws an
                    exception if data has not arrived yet.
                    
        Returns:
            The data sitting on the given in-port name, from the connection 
            associated with the given connection index.
            
        Exceptions:
            Throws an 'IOError' when blocking is disabled and there is no data
            available on the given in-port name, from the connection associated
            with the given connection index.
        '''
        try:
            leakyPipe = inports[inportName][connIndex]
        except KeyError, e:
            # Trying to get data from an unconnected in-port
            logging.info('Data requested from an unconnected port: {proc}.{port}'.format(proc=core['name'],
                                                                                         port=inportName))
            raise e
        conn = scheduler.util.plumber.getConnection(leakyPipe)
        if not block:
            if not conn.poll():
                raise IOError('In-port {proc}.{port} not ready for recv()'.format(proc=core['name'],
                                                                                  port=inportName))
        data = conn.recv()
        logging.debug('RECV: {proc}.{port} = {data}'.format(data=str(data),
                                                            proc=core['name'],
                                                            port=inportName))
        received.add(inportName)
        checkInputs()
        return data
    def getDataFxn(inportName, block=True):
        '''
        Assuming the given in-port name has only one connection, get the next 
        information packet that arrives at that port.  If there is no data at
        the port, this method blocks until data arrives.   
        
        Parameters:
            inportName - The name of the in-port to receive data from.
            block - When 'True', the default, this method blocks until data
                    arrives, on the given in-port name. If 'False', the method
                    returns immediately with data or it throws an exception if
                    data has not arrived yet.
            
        Returns:
            The data sitting on the given in-port name.
        '''
        logging.info('{count} connections.'.format(count=len(inports[inportName])))
        connCount = len(inports[inportName])
        if connCount > 1:
            logging.info('In-port {proc}.{port} has {count} connections, but only one requested.'.format(proc=core['name'],
                                                                                                         port=inportName,
                                                                                                         count=connCount))
        return getDataAtFxn(FIRST_CONN, inportName, block=block)
    def setDataFxn(outportName, data):
        '''
        Send the given information packet (or data object) through the given
        out-port. If there are multiple connections, on that single port, load
        balance across all connections by sending data to the connection that
        has waited the longest to receive data. 
        Note: When there are multiple connections, data is *not* copied and 
              broadcast across all connected targets.   
        
        Parameters:
            outportName - The name of the out-port to send data to.
            data - an information packet (or data object)
        '''
        logging.debug('SEND: {proc}.{port} = {data}'.format(data=str(data),
                                                            proc=core['name'],
                                                            port=outportName))
        try:
            # Load balance across out connection (per port) 
            numSetCalls, numConnections = state['set data count'][outportName]
            roundRobinIndex = numSetCalls % numConnections  
            leakyPipe = outports[outportName][roundRobinIndex]
            state['set data count'][outportName][0] += 1
        except KeyError:
            # Trying to send data to an unconnected out-port
            logging.info('Data ({data}) sent to unconnected port: {proc}.{port}'.format(data=str(data),
                                                                                        proc=core['name'],
                                                                                        port=outportName))
            return
        conn = scheduler.util.plumber.getConnection(leakyPipe)
        conn.send(data)
    def getConfigFxn():
        '''
        Get the configuration data for this process.   
        
        Returns:
            Configuration data in whatever format the process expects.
            Note: Configuration data can be specified in the graph file as 
                  metadata on a process.
            Example:
                  { processName : { 'component' : componentName,
                                    'metadata'  : { 'config' : <whatever format> } } }
        '''
        return core['metadata'].get('config', None)
    # Make helper functions available to process
    core['getDataAt'] = getDataAtFxn
    core['getData']   = getDataFxn
    core['setData']   = setDataFxn
    core['getConfig'] = getConfigFxn
    core['lenAt']     = lenAtFxn
    # Component may have no in-ports so check may succeed
    checkInputs()
    # Run the component logic
    fxn(core)
    # A process "closes" when all its inputs close
    isAllInputsClosed = False
    logging.debug('WAIT: Waiting on {name}\'s in-ports {inports} to close...'.format(name=core['name'], inports=inports.keys()))
    while not isAllInputsClosed:
        status = []
        for portName, leakyPipeList in inports.items():
            for i, leakyPipe in enumerate(leakyPipeList):
                try:
                    core['getDataAt'](i, portName)
                    status.append(False)
                except EOFError:
                    status.append(True)
        isAllInputsClosed = all(status)
    logging.debug('WAIT: Done waiting! Process {name} is shutting down.'.format(name=core['name']))
    # Close all connections
    for ports in [inports, outports, core.get('ports', {})]:
        for portName, leakyPipeList in ports.items():
            for leakyPipe in leakyPipeList:
                logging.debug('CONN: [{pid}] On exit, process "{proc}" closed "{proc}.{port}".'.format(pid=os.getpid(), proc=core['name'], port=portName))        
                conn = scheduler.util.plumber.getConnection(leakyPipe)
                conn.close()
    # Log that this component has finished       
    logging.debug('END : {name}'.format(name=core['name']))